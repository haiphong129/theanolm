#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from collections import OrderedDict
import logging
import numpy
import theano
import theano.tensor as tensor
from theanolm.matrixfunctions import random_weight, orthogonal_weight

class BasicLayer(object):
    """Superclass for Neural Network Layers
    """

    def __init__(self, layer_options, network, profile=False):
        """Saves some attributes that are common to all layers.

        :type layer_options: dict
        :param layer_options: dictionary of layer options

        :type network: Network
        :param network: the network object creating this layer

        :type profile: bool
        :param profile: if set to True, creates a Theano profile object
        """

        self.name = layer_options['name']
        self.input_layers = layer_options['input_layers']

        if 'output_size' in layer_options:
            self.output_size = layer_options['output_size']
        else:
            self.output_size = \
                sum([ x.output_size for x in self.input_layers ])

        logging.debug("- %s name=%s inputs=[%s] size=%d",
            self.__class__.__name__,
            self.name,
            ', '.join([x.name for x in self.input_layers]),
            self.output_size)

        self.network = network
        self._profile = profile
        self.param_init_values = OrderedDict()

    def set_params(self, params):
        self._params = params

    def _get_param(self, param_name):
        return self._params[self.name + '/' + param_name]

    def _init_random_weight(self, param_name, input_size, output_size, scale=None, count=1):
        """Generates a weight matrix from “standard normal” distribution.

        :type input_size: int
        :param input_size: size of the input dimension of the weight

        :type output_size: int
        :param output_size: size of the output dimension of the weight

        :type scale: float
        :param scale: if other than None, the matrix will be scaled by this factor

        :rtype: numpy.ndarray
        :returns: the generated weight matrix
        """

        self.param_init_values[self.name + '/' + param_name] = \
            numpy.concatenate([random_weight(input_size, output_size, scale=0.01)
                               for _ in range(count)],
                              axis=1)

    def _init_orthogonal_weight(self, param_name, input_size, output_size, scale=None, count=1):
        """Generates a weight matrix from “standard normal” distribution. If
        in_size matches out_size, generates an orthogonal matrix.

        :type input_size: int
        :param input_size: size of the input dimension of the weight

        :type output_size: int
        :param output_size: size of the output dimension of the weight

        :type scale: float
        :param scale: if other than None, the matrix will be scaled by this factor,
                      unless an orthogonal matrix is created
        """

        self.param_init_values[self.name + '/' + param_name] = \
            numpy.concatenate([orthogonal_weight(input_size, output_size, scale=0.01)
                               for _ in range(count)],
                              axis=1)

    def _init_bias(self, param_name, size, value=None):
        """Initializes a bias vector with given value.

        If ``value`` is not given, initializes the vector with zero value. If
        ``value``is a list, creates a concatenation of as many vectors as there
        are elements in the list.

        :type param_name: str
        :param param_name: name for the parameter within the layer object; the
                           actual name of the Theano shared variable will be
                           ``<layer name>.<parameter name>``.

        :type size: int
        :param size: number of elements in the vector (or in one subvector, in
                     case ``value`` is a list)

        :type value: float or list of floats
        :param value: the value to initialize the elements to, or a list of
                      values to create a concatenation of vectors
        """

        values = value if isinstance(value, list) else [value]
        subvectors = []
        for subvector_value in values:
            if subvector_value is None:
                subvector = numpy.zeros(size).astype(theano.config.floatX)
            else:
                subvector = numpy.empty(size).astype(theano.config.floatX)
                subvector.fill(subvector_value)
            subvectors.append(subvector)
        self.param_init_values[self.name + '/' + param_name] = \
            numpy.concatenate(subvectors)

    def _tensor_preact(self, input_matrix, param_name):
        weight = self._params[self.name + '/' + param_name + '/W']
        bias = self._params[self.name + '/' + param_name + '/b']
        return tensor.dot(input_matrix, weight) + bias