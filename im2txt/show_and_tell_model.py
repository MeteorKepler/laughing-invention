# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Image-to-text implementation based on http://arxiv.org/abs/1411.4555.

"Show and Tell: A Neural Image Caption Generator"
Oriol Vinyals, Alexander Toshev, Samy Bengio, Dumitru Erhan
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf
import os

from im2txt.ops import image_embedding
from im2txt.ops import image_processing
from im2txt.ops import inputs as input_ops
from im2txt.reference.ssd import SSD300


class ShowAndTellModel(object):
    """Image-to-text implementation based on http://arxiv.org/abs/1411.4555.
  
    "Show and Tell: A Neural Image Caption Generator"
    Oriol Vinyals, Alexander Toshev, Samy Bengio, Dumitru Erhan
    """

    def __init__(self, config, mode, train_inception=False):
        """Basic setup.
    
        Args:
          config: Object containing configuration parameters.
          mode: "train", "eval" or "inference".
          train_inception: Whether the inception submodel variables are trainable.
        """
        assert mode in ["train", "eval", "inference"]
        self.config = config
        self.mode = mode
        self.train_inception = train_inception

        # Reader for the input data.
        self.reader = tf.TFRecordReader()

        # To match the "Show and Tell" paper we initialize all variables with a
        # random uniform initializer.
        self.initializer = tf.random_uniform_initializer(
            minval=-self.config.initializer_scale,
            maxval=self.config.initializer_scale)

        # A float32 Tensor with shape [batch_size, height, width, channels].
        self.images = None

        # A float32 Tensor with shape [batch_size, 300, 300, channels]. For ssd detection.
        self.images_300x300 = None

        # An int32 Tensor with shape [batch_size, padded_length].
        self.input_seqs = None

        # An int32 Tensor with shape [batch_size, padded_length].
        self.target_seqs = None

        # An int32 0/1 Tensor with shape [batch_size, padded_length].
        self.input_mask = None

        # A float32 Tensor with shape [batch_size, embedding_size].
        self.image_embeddings = None

        # A float32 Tensor with shape [batch_size, padded_length, embedding_size].
        self.seq_embeddings = None

        # A float32 Tensor with shape [batch_size, embedding_size].
        self.region_aspect_embeddings = None

        # A float32 Tensor with shape [batch_size, 2 * embedding_size].
        self.image_embeddings_with_aspect = None

        # A float32 Tensor with shape [batch_size, padded_length, 2 * embedding_size].
        self.seq_embeddings_with_aspect = None

        # A float32 scalar Tensor; the total loss for the trainer to optimize.
        self.total_loss = None

        # A float32 Tensor with shape [batch_size * padded_length].
        self.target_cross_entropy_losses = None

        # A float32 Tensor with shape [batch_size * padded_length].
        self.target_cross_entropy_loss_weights = None

        # Collection of variables from the inception submodel.
        self.inception_variables = []

        # Function to restore the inception submodel from checkpoint.
        self.init_fn = None

        # Global step Tensor.
        self.global_step = None

    def is_training(self):
        """Returns true if the model is built for training mode."""
        return self.mode == "train"

    def process_image(self, encoded_image, thread_id=0):
        """Decodes and processes an image string.
    
        Args:
          encoded_image: A scalar string Tensor; the encoded image.
          thread_id: Preprocessing thread id used to select the ordering of color
            distortions.
    
        Returns:
          A float32 Tensor of shape [height, width, 3]; the processed image.
        """
        return image_processing.process_image(encoded_image,
                                              is_training=self.is_training(),
                                              height=self.config.image_height,
                                              width=self.config.image_width,
                                              thread_id=thread_id,
                                              image_format=self.config.image_format)

    def build_inputs(self):
        """Input prefetching, preprocessing and batching.
    
        Outputs:
          self.images
          self.input_seqs
          self.target_seqs (training and eval only)
          self.input_mask (training and eval only)
        """
        if self.mode == "inference":
            # In inference mode, images and inputs are fed via placeholders.
            image_feed = tf.placeholder(dtype=tf.string, shape=[], name="image_feed")
            input_feed = tf.placeholder(dtype=tf.int64,
                                        shape=[None],  # batch_size
                                        name="input_feed")

            # Process image and insert batch dimensions.
            images = tf.expand_dims(self.process_image(image_feed), 0)
            input_seqs = tf.expand_dims(input_feed, 1)

            # No target sequences or input mask in inference mode.
            target_seqs = None
            input_mask = None
        else:
            # Prefetch serialized SequenceExample protos.
            input_queue = input_ops.prefetch_input_data(
                self.reader,
                self.config.input_file_pattern,
                is_training=self.is_training(),
                batch_size=self.config.batch_size,
                values_per_shard=self.config.values_per_input_shard,
                input_queue_capacity_factor=self.config.input_queue_capacity_factor,
                num_reader_threads=self.config.num_input_reader_threads)

            # Image processing and random distortion. Split across multiple threads
            # with each thread applying a slightly different distortion.
            assert self.config.num_preprocess_threads % 2 == 0
            images_and_captions = []
            for thread_id in range(self.config.num_preprocess_threads):
                serialized_sequence_example = input_queue.dequeue()
                encoded_image, caption = input_ops.parse_sequence_example(
                    serialized_sequence_example,
                    image_feature=self.config.image_feature_name,
                    caption_feature=self.config.caption_feature_name)
                image = self.process_image(encoded_image, thread_id=thread_id)
                images_and_captions.append([image, caption])

            # Batch inputs.
            queue_capacity = (2 * self.config.num_preprocess_threads *
                              self.config.batch_size)
            images, input_seqs, target_seqs, input_mask = (
                input_ops.batch_with_dynamic_pad(images_and_captions,
                                                 batch_size=self.config.batch_size,
                                                 queue_capacity=queue_capacity))

        self.images = images
        self.input_seqs = input_seqs
        self.target_seqs = target_seqs
        self.input_mask = input_mask

    def image_inception_embeddings(self, images):
        inception_output = image_embedding.inception_v3(
            images,
            trainable=self.train_inception,
            is_training=self.is_training())
        self.inception_variables = tf.get_collection(
            tf.GraphKeys.GLOBAL_VARIABLES, scope="InceptionV3")

        # Map inception output into embedding space.
        with tf.variable_scope("image_embedding") as scope:
            image_embeddings = tf.contrib.layers.fully_connected(
                inputs=inception_output,
                num_outputs=self.config.embedding_size,
                activation_fn=None,
                weights_initializer=self.initializer,
                biases_initializer=None,
                scope=scope)
            scope.reuse_variables()

        return image_embeddings

    def build_image_embeddings(self):
        """Builds the image model subgraph and generates image embeddings.
    
        Inputs:
          self.images
    
        Outputs:
          self.image_embeddings
        """
        # Save the embedding size in the graph.
        tf.constant(self.config.embedding_size, name="embedding_size")

        self.image_embeddings = self.image_inception_embeddings(self.images)

    def build_image_ssd_embeddings(self):
        self.images_300x300 = tf.image.resize_images(self.images, [300, 300])
        with tf.variable_scope("ssd"):
            ssd = SSD300((300, 300, 3))
            ssd_weights_file = os.path.join(os.path.dirname(self.config.inception_checkpoint_file),
                                            'weights_SSD300.hdf5')
            ssd.load_weights(ssd_weights_file)
            if not self.train_inception:
                for layer in ssd.layers:
                    layer.trainable = False
            ssd_output = ssd(self.images_300x300)
            ssd_output = tf.reshape(ssd_output,
                                    tf.TensorShape([self.images.get_shape()[0], ssd_output.get_shape()[1],
                                                    ssd_output.get_shape()[2]]))

        with tf.variable_scope("ssd_out_processing"):
            mbox_loc = ssd_output[:, :, :4]
            variances = ssd_output[:, :, -4:]
            mbox_priorbox = ssd_output[:, :, -8:-4]
            mbox_conf = ssd_output[:, :, 4:-8]
            prior_width = mbox_priorbox[:, :, 2] - mbox_priorbox[:, :, 0]
            prior_height = mbox_priorbox[:, :, 3] - mbox_priorbox[:, :, 1]
            prior_center_x = 0.5 * (mbox_priorbox[:, :, 2] + mbox_priorbox[:, :, 0])
            prior_center_y = 0.5 * (mbox_priorbox[:, :, 3] + mbox_priorbox[:, :, 1])
            decode_bbox_center_x = mbox_loc[:, :, 0] * prior_width * variances[:, :, 0]
            decode_bbox_center_x += prior_center_x
            decode_bbox_center_y = mbox_loc[:, :, 1] * prior_width * variances[:, :, 1]
            decode_bbox_center_y += prior_center_y
            decode_bbox_width = tf.exp(mbox_loc[:, :, 2] * variances[:, :, 2])
            decode_bbox_width *= prior_width
            decode_bbox_height = tf.exp(mbox_loc[:, :, 3] * variances[:, :, 3])
            decode_bbox_height *= prior_height
            decode_bbox_xmin = tf.expand_dims(decode_bbox_center_x - 0.5 * decode_bbox_width, -1)
            decode_bbox_ymin = tf.expand_dims(decode_bbox_center_y - 0.5 * decode_bbox_height, -1)
            decode_bbox_xmax = tf.expand_dims(decode_bbox_center_x + 0.5 * decode_bbox_width, -1)
            decode_bbox_ymax = tf.expand_dims(decode_bbox_center_y + 0.5 * decode_bbox_height, -1)
            decode_bbox = tf.squeeze(tf.concat((decode_bbox_xmin[:, None],
                                                decode_bbox_ymin[:, None],
                                                decode_bbox_xmax[:, None],
                                                decode_bbox_ymax[:, None]), axis=-1), 1)
            decode_bbox = tf.minimum(tf.maximum(decode_bbox, 0.0), 1.0)

            mbox_conf_max = tf.reduce_max(mbox_conf, 2)

            idx_list = []
            for i, bboxes in enumerate(tf.unstack(decode_bbox)):
                idx_item = tf.image.non_max_suppression(bboxes, mbox_conf_max[i, :], 1)
                idx_list.append(idx_item)
            idx = tf.stack(idx_list)

            good_box_list = []
            for i, idx_item in enumerate(tf.unstack(idx)):
                good_box_item = decode_bbox[i, idx_item[0], :]
                good_box_list.append(good_box_item)
            good_box = tf.stack(good_box_list)

            # In order to make tf.map_fn support multi-params
            # href=http://stackoverflow.com/questions/37086098/does-tensorflow-map-fn-support-taking-more-than-one-tensor
            # def mmap(fn, arrays, dtype=tf.float32):
            #     # assumes all arrays have same leading dim
            #     indices = tf.range(tf.shape(arrays[0])[0])
            #     out = tf.map_fn(lambda ii: fn(*[array[ii] for array in arrays]), indices, dtype=dtype)
            #     return out
            #
            # idx = mmap(lambda bbox, score: tf.image.non_max_suppression(bbox, score, 1),
            #                 [decode_bbox, mbox_conf_max], dtype=tf.int32)
            # idx = tf.reshape(idx, [-1, 1])
            # good_box = mmap(lambda boxes, batch_num: boxes[idx[batch_num, 1], :],
            #                 [decode_bbox, tf.range(0, tf.shape(self.images)[0])], dtype=tf.float32)

        with tf.variable_scope("region_image_generating"):
            region_images = tf.image.crop_and_resize(self.images_300x300,
                                                     boxes=good_box,
                                                     box_ind=tf.range(0, tf.shape(self.images)[0]),
                                                     crop_size=[300, 300])
        with tf.variable_scope("region_image_embedding"):
            region_image_embeddings = self.image_inception_embeddings(region_images)
            self.region_aspect_embeddings = tf.reshape(region_image_embeddings, tf.shape(self.image_embeddings))

    def build_seq_embeddings(self):
        """Builds the input sequence embeddings.
    
        Inputs:
          self.input_seqs
    
        Outputs:
          self.seq_embeddings
        """
        with tf.variable_scope("seq_embedding"), tf.device("/cpu:0"):
            embedding_map = tf.get_variable(
                name="map",
                shape=[self.config.vocab_size, self.config.embedding_size],
                initializer=self.initializer)
            seq_embeddings = tf.nn.embedding_lookup(embedding_map, self.input_seqs)

        self.seq_embeddings = seq_embeddings

    def build_model(self):
        """Builds the model.
    
        Inputs:
          self.image_embeddings
          self.seq_embeddings
          self.target_seqs (training and eval only)
          self.input_mask (training and eval only)
    
        Outputs:
          self.total_loss (training and eval only)
          self.target_cross_entropy_losses (training and eval only)
          self.target_cross_entropy_loss_weights (training and eval only)
        """
        # This LSTM cell has biases and outputs tanh(new_c) * sigmoid(o), but the
        # modified LSTM in the "Show and Tell" paper has no biases and outputs
        # new_c * sigmoid(o).
        lstm_cell = tf.contrib.rnn.BasicLSTMCell(
            num_units=self.config.num_lstm_units, state_is_tuple=True)
        if self.mode == "train":
            lstm_cell = tf.contrib.rnn.DropoutWrapper(
                lstm_cell,
                input_keep_prob=self.config.lstm_dropout_keep_prob,
                output_keep_prob=self.config.lstm_dropout_keep_prob)

        with tf.variable_scope("process_lstm_input", initializer=self.initializer):
            self.image_embeddings_with_aspect = tf.concat([self.image_embeddings, self.region_aspect_embeddings], 1,
                                                          name="image_embeddings_with_aspect")
            expand_region_aspect_embeddings = tf.expand_dims(self.image_embeddings, 1,
                                                             name="expand_region_aspect_embeddings")
            if self.mode == "inference":
                tile_expand_region_aspect_embeddings = tf.tile(expand_region_aspect_embeddings,
                                                               [tf.shape(self.seq_embeddings)[0],
                                                                1, 1],
                                                               name="tile_expand_region_aspect_embeddings")
            else:
                tile_expand_region_aspect_embeddings = tf.tile(expand_region_aspect_embeddings,
                                                               [1, tf.shape(self.seq_embeddings)[1], 1],
                                                               name="tile_expand_region_aspect_embeddings")
            self.seq_embeddings_with_aspect = tf.concat([self.seq_embeddings, tile_expand_region_aspect_embeddings], 2,
                                                        name="seq_embeddings_with_aspect")

        with tf.variable_scope("lstm", initializer=self.initializer) as lstm_scope:
            # Feed the image embeddings to set the initial LSTM state.
            zero_state = lstm_cell.zero_state(
                batch_size=self.image_embeddings.get_shape()[0], dtype=tf.float32)
            _, initial_state = lstm_cell(self.image_embeddings_with_aspect, zero_state)

            # Allow the LSTM variables to be reused.
            lstm_scope.reuse_variables()

            if self.mode == "inference":
                # In inference mode, use concatenated states for convenient feeding and
                # fetching.
                tf.concat(axis=1, values=initial_state, name="initial_state")

                # Placeholder for feeding a batch of concatenated states.
                state_feed = tf.placeholder(dtype=tf.float32,
                                            shape=[None, sum(lstm_cell.state_size)],
                                            name="state_feed")
                state_tuple = tf.split(value=state_feed, num_or_size_splits=2, axis=1)

                # Run a single LSTM step.
                lstm_outputs, state_tuple = lstm_cell(
                    inputs=tf.squeeze(self.seq_embeddings_with_aspect, axis=[1]),
                    state=state_tuple)

                # Concatentate the resulting state.
                tf.concat(axis=1, values=state_tuple, name="state")
            else:
                # Run the batch of sequence embeddings through the LSTM.
                sequence_length = tf.reduce_sum(self.input_mask, 1)
                lstm_outputs, _ = tf.nn.dynamic_rnn(cell=lstm_cell,
                                                    inputs=self.seq_embeddings_with_aspect,
                                                    sequence_length=sequence_length,
                                                    initial_state=initial_state,
                                                    dtype=tf.float32,
                                                    scope=lstm_scope)

        # Stack batches vertically.
        lstm_outputs = tf.reshape(lstm_outputs, [-1, lstm_cell.output_size])

        with tf.variable_scope("logits") as logits_scope:
            logits = tf.contrib.layers.fully_connected(
                inputs=lstm_outputs,
                num_outputs=self.config.vocab_size,
                activation_fn=None,
                weights_initializer=self.initializer,
                scope=logits_scope)

        if self.mode == "inference":
            tf.nn.softmax(logits, name="softmax")
        else:
            targets = tf.reshape(self.target_seqs, [-1])
            weights = tf.to_float(tf.reshape(self.input_mask, [-1]))

            # Compute losses.
            losses = tf.nn.sparse_softmax_cross_entropy_with_logits(labels=targets,
                                                                    logits=logits)
            batch_loss = tf.div(tf.reduce_sum(tf.multiply(losses, weights)),
                                tf.reduce_sum(weights),
                                name="batch_loss")
            tf.losses.add_loss(batch_loss)
            total_loss = tf.losses.get_total_loss()

            # Add summaries.
            tf.summary.scalar("losses/batch_loss", batch_loss)
            tf.summary.scalar("losses/total_loss", total_loss)
            for var in tf.trainable_variables():
                tf.summary.histogram("parameters/" + var.op.name, var)

            self.total_loss = total_loss
            self.target_cross_entropy_losses = losses  # Used in evaluation.
            self.target_cross_entropy_loss_weights = weights  # Used in evaluation.

    def setup_inception_initializer(self):
        """Sets up the function to restore inception variables from checkpoint."""
        if self.mode != "inference":
            # Restore inception variables only.
            saver = tf.train.Saver(self.inception_variables)

            def restore_fn(sess):
                tf.logging.info("Restoring Inception variables from checkpoint file %s",
                                self.config.inception_checkpoint_file)
                saver.restore(sess, self.config.inception_checkpoint_file)

            self.init_fn = restore_fn

    def setup_global_step(self):
        """Sets up the global step Tensor."""
        global_step = tf.Variable(
            initial_value=0,
            name="global_step",
            trainable=False,
            collections=[tf.GraphKeys.GLOBAL_STEP, tf.GraphKeys.GLOBAL_VARIABLES])

        self.global_step = global_step

    def build(self):
        """Creates all ops for training and evaluation."""
        self.build_inputs()
        self.build_image_embeddings()
        self.build_image_ssd_embeddings()
        self.build_seq_embeddings()
        self.build_model()
        self.setup_inception_initializer()
        self.setup_global_step()
