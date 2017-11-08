from config import args
from modules import *

import tensorflow as tf


def _forward_pass(sources, targets, params, reuse=False):
    with tf.variable_scope('forward_pass', reuse=reuse):
        if args.positional_encoding == 'sinusoidal':
            pos_fn = sinusoidal_positional_encoding
        elif args.positional_encoding == 'learned':
            pos_fn = learned_positional_encoding
        else:
            raise ValueError("positional encoding has to be either 'sinusoidal' or 'learned'")

        # Encoder
        with tf.variable_scope('encoder_embedding'):
            encoded = embed_seq(
                sources, params['source_vocab_size'], args.hidden_units, zero_pad=True, scale=True)
        
        with tf.variable_scope('encoder_positional_encoding'):
            encoded += pos_fn(sources, args.hidden_units, zero_pad=False, scale=False)
        
        with tf.variable_scope('encoder_dropout'):
            encoded = tf.layers.dropout(encoded, args.dropout_rate, training=(not reuse))

        for i in range(args.num_blocks):
            with tf.variable_scope('encoder_attn_%d'%i):
                encoded = multihead_attn(queries=encoded, keys=encoded, num_units=args.hidden_units,
                    num_heads=args.num_heads, dropout_rate=args.dropout_rate, causality=False, reuse=reuse,
                    activation=None)
            
            with tf.variable_scope('encoder_feedforward_%d'%i):
                encoded = pointwise_feedforward(encoded, num_units=[4*args.hidden_units, args.hidden_units],
                    activation=params['activation'])

        # Decoder
        decoder_inputs = _decoder_input_pip(targets, params['start_symbol'])

        if not args.tied_embedding:
            with tf.variable_scope('decoder_embedding'):
                decoded = embed_seq(
                    decoder_inputs, params['target_vocab_size'], args.hidden_units, zero_pad=True, scale=True)
        if args.tied_embedding:
            with tf.variable_scope('encoder_embedding', reuse=True):
                decoded = embed_seq(decoder_inputs, params['target_vocab_size'], args.hidden_units,
                    zero_pad=True, scale=True, TIE_SIGNAL=True)
        
        with tf.variable_scope('decoder_positional_encoding'):
            decoded += pos_fn(decoder_inputs, args.hidden_units, zero_pad=False, scale=False)
                
        with tf.variable_scope('decoder_dropout'):
            decoded = tf.layers.dropout(decoded, args.dropout_rate, training=(not reuse))

        for i in range(args.num_blocks):
            with tf.variable_scope('decoder_self_attn_%d'%i):
                decoded = multihead_attn(queries=decoded, keys=decoded, num_units=args.hidden_units,
                    num_heads=args.num_heads, dropout_rate=args.dropout_rate, causality=True, reuse=reuse,
                    activation=None)
            
            with tf.variable_scope('decoder_attn_%d'%i):
                decoded = multihead_attn(queries=decoded, keys=encoded, num_units=args.hidden_units,
                    num_heads=args.num_heads, dropout_rate=args.dropout_rate, causality=False, reuse=reuse,
                    activation=None)
            
            with tf.variable_scope('decoder_feedforward_%d'%i):
                decoded = pointwise_feedforward(decoded, num_units=[4*args.hidden_units, args.hidden_units],
                    activation=params['activation'])
        
        # Output Layer    
        if args.tied_proj_weight == 1:
            b = tf.get_variable(
                'bias', [params['target_vocab_size']], tf.float32, tf.constant_initializer(0.01))
            _scope = 'encoder_embedding' if args.tied_embedding == 1 else 'decoder_embedding'
            with tf.variable_scope(_scope, reuse=True):
                shared_w = tf.get_variable('lookup_table')
            decoded = tf.reshape(decoded, [-1, args.hidden_units])
            logits = tf.nn.xw_plus_b(decoded, tf.transpose(shared_w), b)
            logits = tf.reshape(logits, [tf.shape(sources)[0], -1, params['target_vocab_size']])
        else:
            with tf.variable_scope('output_layer', reuse=reuse):
                logits = tf.layers.dense(decoded, params['target_vocab_size'], reuse=reuse)
        ids = tf.argmax(logits, -1)
        return logits, ids


def _model_fn_train(features, mode, params):
    logits, _ = _forward_pass(features['source'], features['target'], params)
    _, _ = _forward_pass(features['source'], features['target'], params, reuse=True)

    with tf.name_scope('backward'):
        targets = features['target']
        masks = tf.to_float(tf.not_equal(targets, 0))

        if args.label_smoothing == 1:
            loss_op = label_smoothing_sequence_loss(
                logits=logits, targets=targets, weights=masks, label_depth=params['target_vocab_size'])
        else:
            loss_op = tf.contrib.seq2seq.sequence_loss(
                logits=logits, targets=targets, weights=masks)
        
        if args.warmup_steps > 0:
            step_num = tf.train.get_global_step() + 1   # prevents zero global step
            lr = tf.rsqrt(tf.to_float(args.hidden_units)) * tf.minimum(
                tf.rsqrt(tf.to_float(step_num)),
                tf.to_float(step_num) * tf.convert_to_tensor(args.warmup_steps ** (-1.5)))
        else:
            lr = tf.constant(1e-4)
        log_hook = tf.train.LoggingTensorHook({'lr': lr}, every_n_iter=100)
        
        train_op = tf.train.AdamOptimizer(lr).minimize(loss_op, global_step=tf.train.get_global_step())
    return tf.estimator.EstimatorSpec(
        mode=mode, loss=loss_op, train_op=train_op, training_hooks=[log_hook])


def _model_fn_predict(features, mode, params):
    _, _ = _forward_pass(features['source'], features['target'], params)
    _, ids = _forward_pass(features['source'], features['target'], params, reuse=True)
    return tf.estimator.EstimatorSpec(mode=mode, predictions=ids)


def tf_estimator_model_fn(features, labels, mode, params):
    if mode == tf.estimator.ModeKeys.TRAIN:
        return _model_fn_train(features, mode, params)
    if mode == tf.estimator.ModeKeys.PREDICT:
        return _model_fn_predict(features, mode, params)


def _decoder_input_pip(targets, start_symbol):
    start_symbols = tf.cast(tf.fill([tf.shape(targets)[0], 1], start_symbol), tf.int64)
    return tf.concat([start_symbols, targets[:, :-1]], axis=-1)
