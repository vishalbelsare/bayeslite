# -*- coding: utf-8 -*-

#   Copyright (c) 2010-2014, MIT Probabilistic Computing Project
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import bayeslite.core as core

def bayesdb_register_metamodel(bdb, metamodel):
    """Register `metamodel` in `bdb`, creating any necessary tables.

    `metamodel` must not already be registered in any BayesDB."""
    name = metamodel.name()
    if name in bdb.metamodels:
        raise ValueError('Metamodel already registered: %s' % (name,))
    with bdb.savepoint():
        metamodel.register(bdb)
        bdb.metamodels[name] = metamodel

def bayesdb_deregister_metamodel(bdb, metamodel):
    """Deregister `metamodel`, which must have been registered in `bdb`."""
    name = metamodel.name()
    assert name in bdb.metamodels
    assert bdb.metamodels[name] == metamodel
    del bdb.metamodels[name]

class IBayesDBMetamodel(object):
    """BayesDB metamodel interface.

    Subclasses of :class:`IMetamodel` implement the functionality
    needed by probabilistic BQL queries to sample from and inquire
    about the posterior distribution of a generative model conditioned
    on data in a table.  Instances of `IMetamodel` contain any
    in-memory state associated with the metamodel in the database.
    """

    def name(self):
        raise NotImplementedError
    def register(self, bdb):
        raise NotImplementedError
    def create_generator(self, bdb, table, schema, instantiate):
        raise NotImplementedError
    def drop_generator(self, bdb, generator_id):
        raise NotImplementedError
    def rename_column(self, bdb, generator_id, oldname, newname):
        raise NotImplementedError
    def initialize_models(self, bdb, generator_id, modelnos, model_config):
        raise NotImplementedError
    def drop_models(self, bdb, generator_id, modelnos=None):
        raise NotImplementedError
    def analyze_models(self, bdb, generator_id, modelnos=None, iterations=1,
            max_seconds=None, iterations_per_checkpoint=None):
        raise NotImplementedError
    def column_dependence_probability(self, bdb, generator_id, colno0, colno1):
        raise NotImplementedError
    def mutual_information(self, bdb, generator_id, colno0, colno1,
            numsamples=100):
        raise NotImplementedError
    def column_typicality(self, bdb, generator_id, colno):
        raise NotImplementedError
    def column_value_probability(self, bdb, generator_id, colno, value):
        raise NotImplementedError
    def row_similarity(self, bdb, generator_id, rowid, target_rowid, colnos):
        raise NotImplementedError
    def row_typicality(self, bdb, generator_id, rowid):
        raise NotImplementedError
    def row_column_predictive_probability(self, bdb, generator_id, rowid,
            colno):
        raise NotImplementedError
    def infer(self, bdb, generator_id, colno, rowid, threshold,
            numsamples=None):
        value, confidence = self.infer_confidence(bdb, generator_id, colno,
            rowid, numsamples=numsamples)
        if confidence < threshold:
            return None
        return value
    def infer_confidence(self, bdb, generator_id, colno, rowid,
            numsamples=None):
        raise NotImplementedError
    def simulate(self, bdb, generator_id, constraints, colnos,
            numpredictions=1):
        raise NotImplementedError
    def insertmany(self, bdb, generator_id, rows):
        raise NotImplementedError
