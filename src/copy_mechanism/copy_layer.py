from typing import Callable

from tensorflow.python.layers import base

from tensorflow.python.eager import context
from tensorflow.python.estimator import util as estimator_util
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.layers import utils as layers_util
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import variable_scope as vs
from tensorflow.python.ops import variables as tf_variables
from tensorflow.python.platform import tf_logging as logging
from tensorflow.python.util import nest
from tensorflow.python.eager import context
from tensorflow.python.framework import ops
from tensorflow.python.framework import tensor_shape
from tensorflow.python.layers import base
from tensorflow.python.layers import utils
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import init_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn
from tensorflow.python.ops import standard_ops

from tensorflow.contrib.layers import fully_connected

import tensorflow as tf
import sys

from helpers.misc_utils import debug_tensor, debug_shape
from helpers.ops import safe_log


class CopyLayer(base.Layer):
    """Densely-connected layer class.

  This layer implements the operation:
  `outputs = activation(inputs * kernel + bias)`
  Where `activation` is the activation function passed as the `activation`
  argument (if not `None`), `kernel` is a weights matrix created by the layer,
  and `bias` is a bias vector created by the layer
  (only if `use_bias` is `True`).

  Note: if the input to the layer has a rank greater than 2, then it is
  flattened prior to the initial matrix multiply by `kernel`.

  Arguments:
    units: Integer or Long, dimensionality of the output space.
    activation: Activation function (callable). Set it to None to maintain a
      linear activation.
    use_bias: Boolean, whether the layer uses a bias.
    kernel_initializer: Initializer function for the weight matrix.
      If `None` (default), weights are initialized using the default
      initializer used by `tf.get_variable`.
    bias_initializer: Initializer function for the bias.
    kernel_regularizer: Regularizer function for the weight matrix.
    bias_regularizer: Regularizer function for the bias.
    activity_regularizer: Regularizer function for the output.
    kernel_constraint: An optional projection function to be applied to the
        kernel after being updated by an `Optimizer` (e.g. used to implement
        norm constraints or value constraints for layer weights). The function
        must take as input the unprojected variable and must return the
        projected variable (which must have the same shape). Constraints are
        not safe to use when doing asynchronous distributed training.
    bias_constraint: An optional projection function to be applied to the
        bias after being updated by an `Optimizer`.
    trainable: Boolean, if `True` also add variables to the graph collection
      `GraphKeys.TRAINABLE_VARIABLES` (see `tf.Variable`).
    name: String, the name of the layer. Layers with the same name will
      share weights, but to avoid mistakes we require reuse=True in such cases.
    reuse: Boolean, whether to reuse the weights of a previous layer
      by the same name.

  Properties:
    units: Python integer, dimensionality of the output space.
    activation: Activation function (callable).
    use_bias: Boolean, whether the layer uses a bias.
    kernel_initializer: Initializer instance (or name) for the kernel matrix.
    bias_initializer: Initializer instance (or name) for the bias.
    kernel_regularizer: Regularizer instance for the kernel matrix (callable)
    bias_regularizer: Regularizer instance for the bias (callable).
    activity_regularizer: Regularizer instance for the output (callable)
    kernel_constraint: Constraint function for the kernel matrix.
    bias_constraint: Constraint function for the bias.
    kernel: Weight matrix (TensorFlow variable or tensor).
    bias: Bias vector, if applicable (TensorFlow variable or tensor).
  """

    def __init__(self, embedding_dim,
                 units,
                 activation=None,
                 use_bias=False,
                 kernel_initializer=None,
                 bias_initializer=init_ops.zeros_initializer(),
                 kernel_regularizer=None,
                 bias_regularizer=None,
                 activity_regularizer=None,
                 kernel_constraint=None,
                 bias_constraint=None,
                 trainable=True,
                 name=None,
                 source_provider: Callable[[], tf.Tensor] = None,
                 condition_encoding: Callable[[], tf.Tensor] = None,
                 vocab_size=None,
                 **kwargs):
        super(CopyLayer, self).__init__(trainable=trainable, name=name,
                                        activity_regularizer=activity_regularizer,
                                        **kwargs)
        self.vocab_size = vocab_size
        self.source_provider = source_provider
        self.embedding_dim = embedding_dim
        self.units = units
        self.activation = activation
        self.use_bias = use_bias
        self.kernel_initializer = kernel_initializer
        self.bias_initializer = bias_initializer
        self.kernel_regularizer = kernel_regularizer
        self.bias_regularizer = bias_regularizer
        self.kernel_constraint = kernel_constraint
        self.bias_constraint = bias_constraint
        self.input_spec = base.InputSpec(min_ndim=2)

        self.condition_encoding = condition_encoding

    def build(self, input_shape):
        input_shape = tensor_shape.TensorShape(input_shape)
        print("building copy layer")
        # print(input_shape)
        self.built = True

    def call(self, inputs):
        inputs = ops.convert_to_tensor(inputs, dtype=self.dtype)  # batch x len_source+emb_dim
        # inputs = debug_shape(inputs, "inputs")
        # print(inputs)
        #  [batch_size, emb_dim + len_source] in eval,
        #  [len_target, batch_size,emb_dim + len_source] in train
        source = self.source_provider()  # [batch_size, len_source]
        # source = debug_shape(source,"src")

        condition_encoding = self.condition_encoding()
        # condition_encoding = debug_shape(condition_encoding, "cond enc")

        batch_size = tf.shape(source)[0]
        len_source = tf.shape(source)[1]
        shape = tf.shape(inputs)
        is_eval = len(inputs.get_shape()) == 2

        beam_width = tf.constant(1) if is_eval else shape[1]
        # len_target = tf.Print(len_target, [len_target, batch_size, shape[-1]], "input reshape")
        # inputs = tf.reshape(inputs, [-1, shape[-1]])  # [len_target * batch_size, len_source + emb_dim]
        inputs_new = tf.reshape(inputs,
                                [batch_size*beam_width, shape[-1]])  # [len_target, batch_size, len_source + emb_dim]

        # -- [len_target, batch_size, embedding_dim] attention, []
        # -- [len_target, batch_size, len_source] alignments
        # attention, alignments = tf.split(inputs, [self.embedding_dim, -1], axis=1)
        attention, alignments = tf.split(inputs_new, num_or_size_splits=[self.embedding_dim, -1], axis=-1)
        # [len_target, batch_size, vocab_size]
        shortlist = tf.contrib.layers.fully_connected(attention, self.vocab_size, activation_fn=None)

        # attention = debug_shape(attention, "attn")
        # alignments = debug_shape(alignments, "align")
        # print(alignments)
        # shortlist = debug_shape(shortlist, "outputs")

        # pad the alignments to the longest possible source st output vocab is fixed size
        # TODO: Check for non zero alignments outside the seq length
        alignments_padded = tf.pad(alignments, [[0, 0], [0, self.units-tf.shape(alignments)[-1]]], 'CONSTANT')
        # alignments_padded = debug_shape(alignments_padded, "align padded")
        # switch takes st, vt and yt−1 as inputs
        # vt = concat(weighted context encoding at t; condition encoding)
        # st = hidden state at t
        # y_t-1 is previous generated token

        condition_encoding_tiled = tf.contrib.seq2seq.tile_batch(condition_encoding, multiplier=beam_width)

        vt = tf.concat([attention, condition_encoding_tiled], axis=1)
        # NOTE: this is missing the previous input y_t-1 and s_t
        switch_input = tf.concat([vt],axis=1)
        switch_h1 = tf.layers.dense(switch_input, 64, activation=tf.nn.tanh, kernel_initializer=tf.initializers.orthogonal())
        switch_h2 = tf.layers.dense(switch_h1, 64, activation=tf.nn.tanh, kernel_initializer=tf.initializers.orthogonal())
        switch = tf.layers.dense(switch_h2, 1, activation=tf.sigmoid, kernel_initializer=tf.initializers.orthogonal())
        # switch = debug_shape(switch, "switch")
        result = safe_log(tf.concat([(1-switch)*shortlist,switch*alignments_padded], axis=1))

        target_shape = tf.concat([shape[:-1], [-1]], 0)
        result =tf.reshape(result, target_shape)
        # result = debug_shape(result, "res")
        # print(result)
        # exit()
        return result
        # return tf.Print(result, [tf.reduce_max(switch), tf.reduce_max(shortlist),
        #                          tf.reduce_max(alignments)], summarize=10)

    def compute_output_shape(self, input_shape):
        input_shape = tensor_shape.TensorShape(input_shape)
        input_shape = input_shape.with_rank_at_least(2)

        # print(input_shape)
        if input_shape[-1].value is None:
            raise ValueError(
                'The innermost dimension of input_shape must be defined, but saw: %s'
                % input_shape)
        return input_shape[:-1].concatenate(self.units+self.vocab_size)

    # this for older tf versions
    def _compute_output_shape(self, input_shape):
        return self.compute_output_shape(input_shape)

def dense(
        inputs, units,
        activation=None,
        use_bias=True,
        kernel_initializer=None,
        bias_initializer=init_ops.zeros_initializer(),
        kernel_regularizer=None,
        bias_regularizer=None,
        activity_regularizer=None,
        kernel_constraint=None,
        bias_constraint=None,
        trainable=True,
        name=None,
        reuse=None):
    """Functional interface for the densely-connected layer.

  This layer implements the operation:
  `outputs = activation(inputs.kernel + bias)`
  Where `activation` is the activation function passed as the `activation`
  argument (if not `None`), `kernel` is a weights matrix created by the layer,
  and `bias` is a bias vector created by the layer
  (only if `use_bias` is `True`).

  Note: if the `inputs` tensor has a rank greater than 2, then it is
  flattened prior to the initial matrix multiply by `kernel`.

  Arguments:
    inputs: Tensor input.
    units: Integer or Long, dimensionality of the output space.
    activation: Activation function (callable). Set it to None to maintain a
      linear activation.
    use_bias: Boolean, whether the layer uses a bias.
    kernel_initializer: Initializer function for the weight matrix.
      If `None` (default), weights are initialized using the default
      initializer used by `tf.get_variable`.
    bias_initializer: Initializer function for the bias.
    kernel_regularizer: Regularizer function for the weight matrix.
    bias_regularizer: Regularizer function for the bias.
    activity_regularizer: Regularizer function for the output.
    kernel_constraint: An optional projection function to be applied to the
        kernel after being updated by an `Optimizer` (e.g. used to implement
        norm constraints or value constraints for layer weights). The function
        must take as input the unprojected variable and must return the
        projected variable (which must have the same shape). Constraints are
        not safe to use when doing asynchronous distributed training.
    bias_constraint: An optional projection function to be applied to the
        bias after being updated by an `Optimizer`.
    trainable: Boolean, if `True` also add variables to the graph collection
      `GraphKeys.TRAINABLE_VARIABLES` (see `tf.Variable`).
    name: String, the name of the layer.
    reuse: Boolean, whether to reuse the weights of a previous layer
      by the same name.

  Returns:
    Output tensor.

  Raises:
    ValueError: if eager execution is enabled.
  """
    layer = CopyLayer(units,
                      activation=activation,
                      use_bias=use_bias,
                      kernel_initializer=kernel_initializer,
                      bias_initializer=bias_initializer,
                      kernel_regularizer=kernel_regularizer,
                      bias_regularizer=bias_regularizer,
                      activity_regularizer=activity_regularizer,
                      kernel_constraint=kernel_constraint,
                      bias_constraint=bias_constraint,
                      trainable=trainable,
                      name=name,
                      dtype=inputs.dtype.base_dtype,
                      _scope=name,
                      _reuse=reuse)

    print("inside copy layer, yaaay!")
    sys.exit(0)

    return layer.apply(inputs)