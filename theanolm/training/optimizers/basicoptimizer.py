#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from abc import abstractmethod, ABCMeta
import logging
import numpy
import theano
import theano.tensor as tensor
from theanolm.exceptions import IncompatibleStateError, NumberError
from theanolm.matrixfunctions import test_value
from theanolm.debugfunctions import *

class BasicOptimizer(object, metaclass=ABCMeta):
    """Superclass for Neural Network Language Model Optimizers
    """

    def __init__(self, optimization_options, network, profile=False):
        """Creates Theano functions for training a neural network language
        model.

        The subclass constructor is expected to give default values to all the
        required parameters in ``self.param_init_values`` first. This
        constructor will then create the corresponding Theano shared variables,
        and two update functions, ``self.gradient_update_function``, which
        updates the gradient parameters and returns the cost, and
        ``self.model_update_function``, which updates model state given the
        gradients and the learning rate.

        The gradient update functions takes as arguments three matrices:
        1. Word IDs in the shape of a mini-batch. The functions will slice this
           into input and output.
        2. Class IDs in the shape of a mini-batch. The functions will slice this
           into input and output.
        3. Mask in the shape of a mini-batch, but only for the output words (not
           for the first time step).

        :type optimization_options: dict
        :param optimization_options: a dictionary of optimization options

        :type network: Network
        :param network: the neural network object

        :type profile: bool
        :param profile: if set to True, creates a Theano profile object
        """

        self.network = network

        # Create Theano shared variables from the initial parameter values.
        self.params = {name: theano.shared(value, name)
                       for name, value in self.param_init_values.items()}

        float_type = numpy.dtype(theano.config.floatX).type
        self.float_type = float_type

        # numerical stability / smoothing term to prevent divide-by-zero
        if not 'epsilon' in optimization_options:
            raise ValueError("'epsilon' is not given in optimization options.")
        self._epsilon = float_type(optimization_options['epsilon'])

        # learning rate / step size
        if not 'learning_rate' in optimization_options:
            raise ValueError("'learning_rate' is not given in optimization "
                             "options.")
        self.learning_rate = float_type(optimization_options['learning_rate'])

        # weights for training files
        if not 'weights' in optimization_options:
            raise ValueError("'weights' is not given in optimization options.")
        self._weights = optimization_options['weights']

        # maximum norm for parameter updates
        if not 'max_gradient_norm' in optimization_options:
            raise ValueError("'max_gradient_norm' is not given in optimization "
                             "options.")
        else:
            self._max_gradient_norm = float_type(
                optimization_options['max_gradient_norm'])

        # cost function
        if not 'cost_function' in optimization_options:
            raise ValueError("'cost_function' is not given in optimization "
                             "options.")
        else:
            cost_function = optimization_options['cost_function']

        # number of noise samples for noise-contrastive estimation
        if not 'num_noise_samples' in optimization_options:
            raise ValueError("'num_noise_samples' is not given in optimization "
                             "options.")
        else:
            num_noise_samples = optimization_options['num_noise_samples']

        # ignore <unk> tokens?
        if not 'ignore_unk' in optimization_options:
            raise ValueError("'ignore_unk' is not given in optimization "
                             "options.")
        self.ignore_unk = optimization_options['ignore_unk']

        # penalty for <unk> tokens
        if not 'unk_penalty' in optimization_options:
            raise ValueError("'unk_penalty' is not given in optimization "
                             "options.")
        unk_penalty = optimization_options['unk_penalty']

        unk_id = self.network.vocabulary.word_to_id['<unk>']

        # The functions take as input a mini-batch of word IDs and class IDs,
        # and slice input and target IDs for the network.
        batch_word_ids = tensor.matrix('optimizer/batch_word_ids',
                                       dtype='int64')
        batch_word_ids.tag.test_value = test_value(
            size=(101, 16), high=self.network.vocabulary.num_words())
        batch_class_ids = tensor.matrix('optimizer/batch_class_ids',
                                        dtype='int64')
        batch_class_ids.tag.test_value = test_value(
            size=(101, 16), high=self.network.vocabulary.num_classes())

        if cost_function == 'cross-entropy':
            # Derive the symbolic expression for log probability of each word.
            logprobs = tensor.log(self.network.target_probs())
        elif cost_function == 'nce':
            logprobs = self._get_nce_cost(shared_noise=False)
        elif cost_function == 'nce-shared':
            logprobs = self._get_nce_cost(shared_noise=True)
        elif cost_function == 'blackout':
            logprobs = self._get_blackout_cost(shared_noise=False)
        elif cost_function == 'blackout-shared':
            logprobs = self._get_blackout_cost(shared_noise=True)
        else:
            raise ValueError("Invalid cost function requested: `{}'".format(
                             cost_function))

        # If requested, predict <unk> with constant score.
        if not unk_penalty is None:
            unk_mask = tensor.eq(self.network.target_word_ids, unk_id)
            unk_indices = unk_mask.nonzero()
            logprobs = tensor.set_subtensor(logprobs[unk_indices], unk_penalty)
        # Do not predict masked and possibly <unk> tokens. The mask has to be
        # cast to floatX, otherwise the result will be float64 and pulled out
        # from the GPU earlier than necessary.
        mask = self.network.mask
        if self.ignore_unk:
            mask *= tensor.neq(self.network.target_word_ids, unk_id)
        logprobs *= tensor.cast(mask, theano.config.floatX)
        # Cost is the negative log probability normalized by the number of
        # training examples in the mini-batch, so that the gradients will also
        # be normalized by the number of training examples.
        cost = -logprobs.sum() / tensor.cast(mask.sum(), theano.config.floatX)

        # Derive the symbolic expression for updating the gradient with regard
        # to each parameter.
        self._gradient_exprs = \
            tensor.grad(cost, wrt=list(self.network.params.values()))

        # Ignore unused input, because is_training is only used by dropout
        # layer.
        self.gradient_update_function = theano.function(
            [batch_word_ids, batch_class_ids, self.network.mask],
            cost,
            givens=[(network.input_word_ids, batch_word_ids[:-1]),
                    (network.input_class_ids, batch_class_ids[:-1]),
                    (network.target_word_ids, batch_word_ids[1:]),
                    (network.target_class_ids, batch_class_ids[1:]),
                    (self.network.is_training, numpy.int8(1)),
                    (self.network.num_noise_samples,
                     numpy.int64(num_noise_samples))],
            updates=self._gradient_update_exprs(),
            name='gradient_update_function',
            on_unused_input='ignore',
            profile=profile)

        alpha = tensor.scalar('optimizer/update_weight',
                              dtype=theano.config.floatX)
        alpha.tag.test_value = 0.1
        self.model_update_function = theano.function(
            [alpha],
            [],
            updates=self._model_update_exprs(alpha),
            name='model_update_function',
            profile=profile)

    def get_state(self, state):
        """Pulls parameter values from Theano shared variables.

        If there already is a parameter in the state, it will be replaced, so it
        has to have the same number of elements.

        :type state: h5py.File
        :param state: HDF5 file for storing the optimization parameters
        """

        h5_optimizer = state.require_group('optimizer')
        h5_optimizer.attrs['learning_rate'] = self.learning_rate

        for name, param in self.params.items():
            if name in state:
                state[name][:] = param.get_value()
            else:
                state.create_dataset(name, data=param.get_value())

    def set_state(self, state):
        """Sets the values of Theano shared variables.

        Requires that ``state`` contains values for all the optimization
        parameters.

        :type state: h5py.File
        :param state: HDF5 file that contains the optimization parameters
        """

        if not 'optimizer' in state:
            raise IncompatibleStateError("Optimizer state is missing.")
        h5_optimizer = state['optimizer']

        if not 'learning_rate' in h5_optimizer.attrs:
            raise IncompatibleStateError("Learning rate is missing from "
                                         "optimizer state.")
        self.learning_rate = h5_optimizer.attrs['learning_rate']

        for name, param in self.params.items():
            if not name in state:
                raise IncompatibleStateError("Parameter %s is missing from "
                                             "training state." % name)
            new_value = state[name].value
            param.set_value(new_value)
            if len(new_value.shape) == 0:
                logging.debug("%s <- %s", name, str(new_value))
            else:
                logging.debug("%s <- array%s", name, str(new_value.shape))

    def update_minibatch(self, word_ids, class_ids, file_ids, mask):
        """Optimizes the neural network parameters using the given inputs and
        learning rate.

        :type word_ids: ndarray of ints
        :param word_ids: a 2-dimensional matrix, indexed by time step and
                         sequence, that contains the word IDs

        :type class_ids: ndarray of ints
        :param class_ids: a 2-dimensional matrix, indexed by time step and
                          sequence, that contains the class IDs

        :type file_ids: ndarray of ints
        :param file_ids: a 2-dimensional matrix, indexed by time step and
                         sequence, that identifies the file in case of multiple
                         training files

        :type mask: numpy.ndarray of a floating point type
        :param mask: a 2-dimensional matrix, indexed by time step and sequence,
                     that masks out elements past the sequence ends.
        """

        # We should predict probabilities of the words at the following time
        # step.
        target_word_ids = word_ids[1:]
        target_class_ids = class_ids[1:]
        mask = mask[1:]
        self.update_cost = \
            self.gradient_update_function(word_ids, class_ids, mask)
        if numpy.isnan(self.update_cost) or numpy.isinf(self.update_cost):
            raise NumberError("Mini-batch cost computation resulted in a "
                              "numerical error.")

        alpha = self.learning_rate
        if self.ignore_unk:
            mask *= tensor.neq(target_word_ids, unk_id)
        num_words = numpy.count_nonzero(mask)
        float_type = numpy.dtype(theano.config.floatX).type
        if num_words > 0:
            file_ids = file_ids[:-1]
            weights = self._weights[file_ids]
            alpha *= weights[mask == 1].sum() / float_type(num_words)
        self.model_update_function(alpha)

    def _get_nce_cost(self, shared_noise):
        """Returns a tensor variable that represents the mini-batch cost as
        defined by noise-contrastive estimation.

        M. U. Gutmann (2012)
        Noise-Contrastive Estimation of Unnormalized Statistical Models, with
        Applications to Natural Image Statistics
        http://www.jmlr.org/papers/v13/gutmann12a.html

        :type shared_noise: bool
        :param shared_noise: use shared noise samples across mini-batch
        """

        target_logprobs = self.network.unnormalized_logprobs()
        target_class_ids = self.network.target_class_ids
        if self.network.noise_probs is None:
            word_prob = 1.0 / self.network.vocabulary.num_classes()
            word_logprob = numpy.log(word_prob)
            word_logprob = self.float_type(word_logprob)
            target_prior_logprobs = word_logprob
            # target_prior_logprobs will be broadcasted to the mini-batch shape,
            # when subtracted from target_logprobs.
        else:
            noise_probs = self.network.noise_probs[target_class_ids]
            target_prior_logprobs = tensor.log(noise_probs + self._epsilon)
        # In the article, h = 1 / (1 + e^-G). log(h) can be expressed using the
        # softplus function: log(h) = -log(1 + e^-G) = -softplus(-G)
        G = target_logprobs - target_prior_logprobs
        target_log_h = -tensor.nnet.softplus(-G)

        if shared_noise:
            sample, sample_logprobs = self.network.shared_noise_sample()
            # sample_prior_logprobs will be a one-dimensional array (or a scalar
            # in case of uniform noise), but it will be broadcasted when
            # subtracted from sample_logprobs.
        else:
            sample, sample_logprobs = self.network.noise_sample()
        if self.network.noise_probs is None:
            sample_prior_logprobs = word_logprob
        else:
            noise_probs = self.network.noise_probs[sample]
            sample_prior_logprobs = tensor.log(noise_probs + self._epsilon)
        # log(1 - h) = log(1 - e^G / (e^G + 1))
        #            = log((e^G + 1 - e^G) / (e^G + 1))
        #            = log(1) - log(e^G + 1)
        #            = -softplus(G)
        G = sample_logprobs - sample_prior_logprobs
        sample_log_one_minus_h = -tensor.nnet.softplus(G)
        return target_log_h + sample_log_one_minus_h.sum(2)

    def _get_blackout_cost(self, shared_noise):
        """Returns a tensor variable that represents the mini-batch cost as
        defined by BlackOut.

        S. Ji (2016)
        BlackOut: Speeding up Recurrent Neural Network Language Models With Very
        Large Vocabularies
        https://arxiv.org/abs/1511.06909

        :type shared_noise: bool
        :param shared_noise: use shared noise samples across mini-batch
        """

        target_logprobs = self.network.unnormalized_logprobs()
        target_probs = tensor.exp(target_logprobs)
        if self.network.noise_probs is None:
            word_prob = 1.0 / self.network.vocabulary.num_classes()
            target_prior_probs = word_prob
            # target_prior_probs will be broadcasted to the mini-batch shape,
            # when it is used to divide target_probs.
        else:
            target_prior_probs = \
                self.network.noise_probs[target_class_ids]
        target_weighted_probs = target_probs / target_prior_probs

        if shared_noise:
            sample, sample_logprobs = self.network.shared_noise_sample()
            # sample_prior_probs will be a one-dimensional array (or a scalar in
            # case of uniform noise), but it will be broadcasted when used to
            # divide sample_probs.
        else:
            sample, sample_logprobs = self.network.noise_sample()
        sample_probs = tensor.exp(sample_logprobs)
        if self.network.noise_probs is None:
            sample_prior_probs = word_prob
        else:
            sample_prior_probs = self.network.noise_probs[sample]
        sample_weighted_probs = sample_probs / sample_prior_probs

        denominators = target_weighted_probs + \
                       sample_weighted_probs.sum(2)
        target_costs = target_weighted_probs / denominators
        sample_costs = sample_weighted_probs / denominators[:,:,None]
        sample_costs = 1.0 - sample_costs
        logprobs = tensor.log(target_costs + self._epsilon)
        logprobs += tensor.log(sample_costs + self._epsilon).sum(2)

    @abstractmethod
    def _gradient_update_exprs(self):
        """Returns Theano expressions for updating the any gradient variables
        needed by the optimizer. Implemented by every optimizer subclass.

        :rtype: iterable over pairs (shared variable, new expression)
        :returns: expressions how to update the gradient variables
        """

        assert False

    @abstractmethod
    def _model_update_exprs(self, alpha):
        """Returns Theano expressions for updating the model parameter, given
        the gradient expressions. Implemented by every optimizer subclass.

        :type alpha: TensorVariable
        :param alpha: a scale to be applied to the parameter updates

        :rtype: iterable over pairs (shared variable, new expression)
        :returns: expressions how to update the model parameters
        """

        assert False

    def _normalize(self, updates):
        """Normalizes the norm of a parameter update to given maximum value.

        :type updates: dict of str to TensorVariable
        :param updates: dictionary of symbolic variables that describe the
                        negative gradient of each parameter, after any
                        optimization method specific adaptation

        :rtype: dict of str to TensorVariable
        :returns: dictionary of symbolic variables that describe ``updates``
                  after normalization has been applied
        """

        max_norm = self._max_gradient_norm
        if max_norm is None:
            return

        squares = [tensor.sqr(update) for update in updates.values()]
        sums = [tensor.sum(square) for square in squares]
        total_sum = sum(sums)  # sum over parameter variables
        norm = tensor.sqrt(total_sum)
        target_norm = tensor.clip(norm, 0.0, max_norm)
        for name, update in updates.items():
            updates[name] = update * target_norm / (self._epsilon + norm)
