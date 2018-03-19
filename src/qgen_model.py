# https://arxiv.org/pdf/1705.02012.pdf

import numpy as np
import tensorflow as tf


from squad_model import SQuADModel
from helpers.loader import OOV, PAD, EOS, SOS

def ids_to_string(rev_vocab):
    def _ids_to_string(ids):
        row_str=[]
        for row in ids:
            out_str = []
            for i in row:
                out_str.append(rev_vocab[i])
            row_str.append(out_str)
        return np.asarray(row_str)
    return _ids_to_string

def id_tensor_to_string(ids, rev_vocab):

    return tf.py_func(ids_to_string(rev_vocab), [ids], tf.string)


class QGenMaluuba(SQuADModel):
    def __init__(self, vocab, batch_size):
        self.embedding_size = tf.app.flags.FLAGS.embedding_size
        self.context_encoder_units = tf.app.flags.FLAGS.context_encoder_units
        self.answer_encoder_units = tf.app.flags.FLAGS.answer_encoder_units
        self.decoder_units = tf.app.flags.FLAGS.decoder_units
        super().__init__(vocab, batch_size)

    def build_model(self):

        self.build_data_pipeline(self.batch_size)

        curr_batch_size = tf.shape(self.answer_ids)[0]

        # self.W = tf.get_variable('testvar', [len(self.vocab), len(self.vocab)], initializer=tf.orthogonal_initializer)
        #
        # a_oh = tf.one_hot(tf.mod(self.answer_ids, len(self.vocab)), depth=len(self.vocab))
        # s = tf.shape(a_oh)
        # x = tf.reshape(a_oh, [-1, len(self.vocab)])
        # self.answer_hat = tf.reshape(tf.matmul(x, self.W), s)

        # build teacher output - coerce to vocab and pad with SOS/EOS
        # also build output for loss - one hot over vocab+context
        self.question_onehot = tf.one_hot(self.question_ids, depth=tf.constant(len(self.vocab), shape=[self.batch_size])+self.context_length)
        self.question_coerced = tf.where(tf.greater_equal(self.question_ids, len(self.vocab)), tf.tile(tf.constant([[self.vocab[OOV]]]), tf.shape(self.question_ids)), self.question_ids)
        self.question_teach = tf.concat([tf.tile(tf.constant(self.vocab[SOS], shape=[1, 1]), [curr_batch_size,1]), self.question_coerced[:,:-1]], axis=1)

        # TODO: augment doc embeddings with in(answer)=true

        # Embed c,q,a
        self.embeddings = tf.get_variable('word_embeddings', [len(self.vocab), self.embedding_size], initializer=tf.orthogonal_initializer)

        # First, coerce them to the shortlist vocab. Then embed
        self.context_coerced = tf.where(tf.greater_equal(self.context_ids, len(self.vocab)), tf.tile(tf.constant([[self.vocab[OOV]]]), tf.shape(self.context_ids)), self.context_ids)
        self.context_embedded = tf.nn.embedding_lookup(self.embeddings, self.context_coerced)

        self.question_teach_embedded = tf.nn.embedding_lookup(self.embeddings, self.question_teach)
        self.question_embedded = tf.nn.embedding_lookup(self.embeddings, self.question_coerced)

        self.answer_coerced = tf.where(tf.greater_equal(self.answer_ids, len(self.vocab)), tf.tile(tf.constant([[self.vocab[OOV]]]), tf.shape(self.answer_ids)), self.answer_ids)
        self.answer_embedded = tf.nn.embedding_lookup(self.embeddings, self.answer_coerced) # batch x seq x embed

        # Build encoder for context
        # Build RNN cell for encoder
        with tf.variable_scope('context_encoder'):
            context_encoder_cell_fwd = tf.nn.rnn_cell.MultiRNNCell([tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.context_encoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.))) for n in range(1)])
            context_encoder_cell_bwd = tf.nn.rnn_cell.MultiRNNCell([tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.context_encoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.))) for n in range(1)])

            # Unroll encoder RNN
            context_encoder_output_parts, context_encoder_state = tf.nn.bidirectional_dynamic_rnn(
                context_encoder_cell_fwd, context_encoder_cell_bwd, self.context_embedded,
                sequence_length=self.context_length, dtype=tf.float32)
            self.context_encoder_output = tf.concat([context_encoder_output_parts[0], context_encoder_output_parts[1]], axis=2) # batch x seq x 2*units


        # Build encoder for mean(encoder(context)) + answer
        # Build RNN cell for encoder
        with tf.variable_scope('a_encoder'):
            # To build the "extractive condition encoding" input, take embeddings of answer words concated with encoded context at that position

            # This is super involved! Even though we have the right indices we have to do a LOT of massaging to get them in the right shape
            seq_length = tf.reduce_max(self.answer_length)
            self.indices = tf.concat([[tf.range(self.answer_pos[i], self.answer_pos[i]+tf.reduce_max(self.answer_length)) for i in range(self.batch_size)]], axis=1)
            # cap the indices to be valid
            self.indices = tf.minimum(self.indices, tf.tile(tf.expand_dims(self.context_length-1,axis=1),[1,tf.reduce_max(self.answer_length)]))

            batch_ix = tf.expand_dims(tf.transpose(tf.tile(tf.expand_dims(tf.range(curr_batch_size),axis=0),[seq_length,1]),[1,0]),axis=2)
            full_ix = tf.concat([batch_ix,tf.expand_dims(self.indices,axis=-1)], axis=2)
            self.context_condition_encoding = tf.gather_nd(self.context_encoder_output, full_ix)


            self.full_condition_encoding = tf.concat([self.context_condition_encoding, self.answer_embedded], axis=2)

            a_encoder_cell_fwd = tf.nn.rnn_cell.MultiRNNCell([tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.context_encoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.))) for n in range(1)])
            a_encoder_cell_bwd = tf.nn.rnn_cell.MultiRNNCell([tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.context_encoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.))) for n in range(1)])

            # Unroll encoder RNN
            a_encoder_output_parts, a_encoder_state_parts = tf.nn.bidirectional_dynamic_rnn(
                a_encoder_cell_fwd, a_encoder_cell_bwd, self.full_condition_encoding,
                sequence_length=self.answer_length, dtype=tf.float32)

            self.a_encoder_final_state = tf.concat([a_encoder_state_parts[0][0].c, a_encoder_state_parts[1][0].c], axis=1) # batch x 2*a_encoder_units

        # concat direction outputs again

        # build init state
        with tf.variable_scope('decoder_initial_state'):
            L = tf.get_variable('decoder_L', [self.context_encoder_units*2, self.context_encoder_units*2], initializer=tf.orthogonal_initializer(), dtype=tf.float32)
            W0 = tf.get_variable('decoder_W0', [self.context_encoder_units*2, self.decoder_units], initializer=tf.orthogonal_initializer(), dtype=tf.float32)
            b0 = tf.get_variable('decoder_b0', [self.decoder_units], initializer=tf.zeros_initializer(), dtype=tf.float32)

            r = tf.reduce_sum(self.context_encoder_output, axis=1)/tf.tile(tf.expand_dims(tf.cast(self.context_length,tf.float32),axis=1),[1,self.context_encoder_units*2]) + tf.matmul(self.a_encoder_final_state,L)
            self.s0 = tf.nn.tanh(tf.matmul(r,W0) + b0)

        # decode
        with tf.variable_scope('decoder'):
            init_state = tf.contrib.rnn.LSTMStateTuple(self.s0, tf.zeros([curr_batch_size, self.decoder_units]))


            attention_mechanism = tf.contrib.seq2seq.BahdanauAttention(
                            num_units=self.decoder_units, memory=self.context_encoder_output,
                            memory_sequence_length=self.context_length)
            decoder_cell = tf.contrib.rnn.DropoutWrapper(
                    cell=tf.contrib.rnn.BasicLSTMCell(num_units=self.decoder_units),
                    input_keep_prob=(tf.cond(self.is_training,lambda: 1.0 - self.dropout_prob,lambda: 1.)))
            decoder_cell = tf.contrib.seq2seq.AttentionWrapper(decoder_cell, attention_mechanism, attention_layer_size=self.decoder_units / 2, alignment_history=True)


            # Helper - training
            helper = tf.contrib.seq2seq.TrainingHelper(
                self.question_teach_embedded, self.question_length)
                # decoder_emb_inp, length(decoder_emb_inp)+1)

            # Decoder - training
            decoder = tf.contrib.seq2seq.BasicDecoder(
                decoder_cell, helper,
                initial_state=decoder_cell.zero_state(curr_batch_size, tf.float32).clone(cell_state=init_state)
                # initial_state=encoder_state
                )

            # Unroll the decoder
            outputs, decoder_states,out_lens = tf.contrib.seq2seq.dynamic_decode(decoder,impute_finished=True, maximum_iterations=tf.reduce_max(self.question_length))

            projection_layer = tf.layers.Dense(
                len(self.vocab), use_bias=False)
            logits = projection_layer(outputs.rnn_output)

            target_weights = tf.sequence_mask(
                        self.question_length, tf.reduce_max(self.question_length), dtype=logits.dtype)

            crossent = tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=self.question_coerced, logits=logits)
            self.loss = tf.reduce_mean(tf.reduce_sum(crossent * target_weights,axis=1),axis=0)

            self.attention = tf.transpose(decoder_states.alignment_history.stack(),[1,0,2]) # batch x seq x attn

        # calc switch prob
        with tf.variable_scope('switch'):
            context = tf.matmul( self.attention, self.context_embedded)
            self.switch = tf.layers.dense(tf.concat([context, outputs.rnn_output],axis=2), 1, activation=tf.sigmoid)

        # get pointer location
        # with tf.variable_scope('pointer'):


        # build overall prediction prob vector

        self.q_hat = tf.nn.softmax(logits,axis=2)

        self.a_string = id_tensor_to_string(self.answer_coerced, self.rev_vocab)
        self.q_hat_string = id_tensor_to_string(tf.argmax(self.q_hat,axis=2,output_type=tf.int32), self.rev_vocab)
        q_gold = id_tensor_to_string(self.question_coerced, self.rev_vocab)
        self._output_summaries.extend(
            [tf.summary.text("q_hat", self.q_hat_string),
            tf.summary.text("q_gold", q_gold),
            # tf.summary.text("q_gold_ids", tf.as_string(self.question_ids)),
            # tf.summary.text("q_raw", self.question_raw),
            # tf.summary.text("context", self.context_raw),
            tf.summary.text("answer", self.answer_raw)])

        # Calculate and clip gradients
        params = tf.trainable_variables()
        gradients = tf.gradients(self.loss, params)
        clipped_gradients, _ = tf.clip_by_global_norm(
            gradients, 5)

        # Optimization
        self.optimizer = tf.train.AdamOptimizer(0.001).apply_gradients(
            zip(clipped_gradients, params))

        self.accuracy = tf.reduce_mean(tf.cast(tf.equal(self.question_coerced,tf.argmax(self.q_hat,axis=2,output_type=tf.int32)),tf.float32))

    def predict(self):
        return self.answer_hat