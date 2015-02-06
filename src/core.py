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

# pylint: disable=star-args

# Implementation notes:
#
# - Use SQL parameters to pass strings and other values into SQL.
#   Don't use %.
#
#      DO:      db.execute("UPDATE foo SET x = ? WHERE id = ?", (x, id))
#      DON'T:   db.execute("UPDATE foo SET x = '%s' WHERE id = %d" % (x, id))
#
# - Use sqlite3_quote_name and % to make SQL queries that refer to
#   tables, columns, &c.
#
#      DO:      qt = sqlite3_quote_name(table)
#               qc = sqlite3_quote_name(column)
#               db.execute("SELECT %s FROM %s WHERE x = ?" % (qc, qt), (x,))
#      DON'T:   db.execute("SELECT %s FROM %s WHERE x = ?" % (column, table),
#                   (x,))

import contextlib
import json
import math
import sqlite3

from bayeslite.sqlite3_util import sqlite3_exec_1
from bayeslite.sqlite3_util import sqlite3_quote_name

from bayeslite.util import arithmetic_mean
from bayeslite.util import casefold
from bayeslite.util import unique
from bayeslite.util import unique_indices

bayesdb_type_table = [
    # column type, numerical?, default sqlite, default model type
    ("categorical",     False,  "text",         "symmetric_dirichlet_discrete"),
    ("cyclic",          True,   "real",         "vonmises"),
    ("key",             False,  "text",         None),
    ("numerical",       True,   "real",         "normal_inverse_gamma"),
]

# XXX What about other model types from the paper?
#
# asymmetric_beta_bernoulli
# pitmanyor_atom
# poisson_gamma
#
# XXX Upgrade column types:
#       continuous -> numerical
#       multinomial -> categorical.

### BayesDB class interface

class IBayesDB(object):
    """Interface of Bayesian databases."""

    def __init__(self, engine):
        self.engine = engine
        self.sqlite = None
        self.txn_depth = 0
        self.metadata_cache = None
        self.models_cache = None
        raise NotImplementedError

    def close(self):
        """Close the database.  Further use is not allowed."""
        raise NotImplementedError

    def cursor(self):
        """Return a cursor fit for executing BQL queries."""
        raise NotImplementedError

    def execute(self, query, *args):
        """Execute a BQL query and return a cursor for its results."""
        raise NotImplementedError

    @contextlib.contextmanager
    def savepoint(self):
        """Savepoint context.  On return, commit; on exception, roll back.

        Savepoints may be nested.
        """
        raise NotImplementedError

def bayesdb_install_bql(db, cookie):
    def function(name, nargs, fn):
        db.create_function(name, nargs,
            lambda *args: bayesdb_bql(fn, cookie, *args))
    function("bql_column_correlation", 3, bql_column_correlation)
    function("bql_column_dependence_probability", 3,
        bql_column_dependence_probability)
    function("bql_column_mutual_information", 3, bql_column_mutual_information)
    function("bql_column_typicality", 2, bql_column_typicality)
    function("bql_column_value_probability", 3, bql_column_value_probability)
    function("bql_row_similarity", -1, bql_row_similarity)
    function("bql_row_typicality", 2, bql_row_typicality)
    function("bql_row_column_predictive_probability", 3,
        bql_row_column_predictive_probability)
    function("bql_infer", 5, bql_infer)

# XXX XXX XXX Temporary debugging kludge!
import traceback

def bayesdb_bql(fn, cookie, *args):
    try:
        return fn(cookie, *args)
    except Exception as e:
        print traceback.format_exc()
        raise e

### Importing SQLite tables

# XXX Use URIs, and attach read-only?
def bayesdb_attach_sqlite_file(bdb, name, pathname):
    """Attach the SQLite database file at PATHNAME under the name NAME."""
    # Turns out Python urlparse is broken and can't handle relative
    # pathnames.  Urgh.
    #uri = urlparse.urlunparse("file", "", urllib.quote(pathname), "", "", "")
    bdb.sqlite.execute("ATTACH DATABASE ? AS %s" % (name,), pathname)

# def bayesdb_attach_sqlite_uri(bdb, name, uri):
#     ...

# XXX Accept parameters for guessing column types: count_cutoff &c.
#
# XXX Allow ignored columns?
def bayesdb_import_sqlite_table(bdb, table,
        column_names=None, column_types=None):
    """Import a SQLite table for use in a BayesDB with BQL.

    COLUMN_NAMES is a list specifying the desired order and selection
    of column names for the BQL table.  COLUMN_TYPES is a dict mapping
    the selected column names to their BQL column types.
    """
    column_names, column_types = bayesdb_determine_columns(bdb, table,
        column_names, column_types)
    metadata = bayesdb_create_metadata(bdb, table, column_names, column_types)
    metadata_json = json.dumps(metadata)
    # XXX Check that rowids are contiguous.
    with bdb.savepoint():
        table_sql = "INSERT INTO bayesdb_table (name, metadata) VALUES (?, ?)"
        table_cursor = bdb.sqlite.execute(table_sql, (table, metadata_json))
        table_id = table_cursor.lastrowid
        assert table_id is not None
        for colno, name in enumerate(column_names):
            column_sql = """
                INSERT INTO bayesdb_table_column (table_id, name, colno)
                VALUES (?, ?, ?)
            """
            bdb.sqlite.execute(column_sql, (table_id, name, colno))

def bayesdb_determine_columns(bdb, table, column_names, column_types):
    # Find the columns as SQLite knows about them.
    qt = sqlite3_quote_name(table)
    cursor = bdb.sqlite.execute("PRAGMA table_info(%s)" % (qt,))
    column_descs = cursor.fetchall()

    # Determine the column names, which must all be among the ones
    # SQLite knows about.
    if column_names is None:
        column_names = [name for _i, name, _t, _n, _d, _p in column_descs]
    else:
        column_name_superset = set(casefold(name)
            for _i, name, _t, _n, _d, _p in column_descs)
        assert len(column_name_superset) == len(column_descs)
        for name in column_names:
            if casefold(name) not in column_name_superset:
                raise ValueError("Unknown column named: %s" % (name,))

    # Make sure there are no names that differ only by case.
    column_names_folded = {}
    for name in column_names:
        if casefold(name) in column_names_folded:
            raise ValueError("Column names differ only in case: %s, %s" %
                (column_names_folded[casefold(name)], name))
        column_names_folded[casefold(name)] = name

    # Determine the column types.  If none are given, guess them from
    # the data in the table; otherwise, make sure every name in
    # column_types appears in the list of column names.
    if column_types is None:
        column_types = dict(bayesdb_guess_column(bdb, table, desc)
            for desc in column_descs
            if casefold(desc[1]) in column_names_folded)
    else:
        for name in column_types:
            if casefold(name) not in column_names_folded:
                raise ValueError("Unknown column typed: %s" % (name,))

    # Make sure every column named is given a type.
    column_types_folded = dict((casefold(name), column_types[name])
        for name in column_types)
    for name in column_names_folded:
        if name not in column_types_folded:
            raise ValueError("Named column missing type: %s" % (name,))

    # Make sure there's at most one key column.
    key = None
    for name in column_types:
        if column_types[name] == "key":
            if key is None:
                key = name
            else:
                raise ValueError("More than one key: %s, %s" % (key, name))

    # Rip out the key column and the ignored columns.
    modelled_column_names = [name for name in column_names
        if column_types_folded[casefold(name)] not in ("key", "ignore")]
    modelled_column_types = dict((name, column_types[name])
        for name in column_types
        if column_types[name] not in ("key", "ignore"))

    # Make sure at least one column is modelled.
    if len(modelled_column_names) == 0:
        raise ValueError("No columns to model")

    return modelled_column_names, modelled_column_types

# XXX Pass count_cutoff/ratio_cutoff through from above.
def bayesdb_guess_column(bdb, table, column_desc,
        count_cutoff=20, ratio_cutoff=0.02):
    (_cid, column_name, _sql_type, _nonnull, _default, primary_key) = \
        column_desc
    # XXXX Can we ask about unique constraints too?
    if primary_key:
        return (column_name, "key")
    # XXX Use sqlite column type as a heuristic?  Won't help for CSV.
    qt = sqlite3_quote_name(table)
    qcn = sqlite3_quote_name(column_name)
    ndistinct_sql = "SELECT COUNT(DISTINCT %s) FROM %s" % (qcn, qt)
    ndistinct = sqlite3_exec_1(bdb.sqlite, ndistinct_sql)
    if ndistinct <= count_cutoff:
        return (column_name, "categorical")
    ndata_sql = "SELECT COUNT(%s) FROM %s" % (qcn, qt)
    ndata = sqlite3_exec_1(bdb.sqlite, ndata_sql)
    if (float(ndistinct) / float(ndata)) <= ratio_cutoff:
        return (column_name, "categorical")
    if not bayesdb_column_floatable_p(bdb, table, column_desc):
        return (column_name, "categorical")
    return (column_name, "numerical")

# XXX This is a kludge!
def bayesdb_column_floatable_p(bdb, table, column_desc):
    (_cid, column_name, _sql_type, _nonnull, _default, _primary_key) = \
        column_desc
    qt = sqlite3_quote_name(table)
    qcn = sqlite3_quote_name(column_name)
    sql = "SELECT %s FROM %s WHERE %s IS NOT NULL" % (qcn, qt, qcn)
    cursor = bdb.sqlite.execute(sql)
    try:
        for row in cursor:
            float(row[0])
    except ValueError:
        return False
    return True

def bayesdb_create_metadata(bdb, table, column_names, column_types):
    ncols = len(column_names)
    assert ncols == len(column_types)
    # Weird contortions to ignore case distinctions in column_names
    # and the keys of column_types.
    column_positions = dict((casefold(name), i)
        for i, name in enumerate(column_names))
    column_metadata = [None] * ncols
    for name in column_types:
        metadata = metadata_generators[column_types[name]](bdb, table, name)
        column_metadata[column_positions[casefold(name)]] = metadata
    assert all(metadata is not None for metadata in column_metadata)
    return {
        "name_to_idx": dict(zip(column_names, range(ncols))),
        "idx_to_name": dict(zip(map(unicode, range(ncols)), column_names)),
        "column_metadata": column_metadata,
    }

def bayesdb_metadata_numerical(_bdb, _table, _column_name):
    return {
        "modeltype": "normal_inverse_gamma",
        "value_to_code": {},
        "code_to_value": {},
    }

def bayesdb_metadata_cyclic(_bdb, _table, _column_name):
    return {
        "modeltype": "vonmises",
        "value_to_code": {},
        "code_to_value": {},
    }

def bayesdb_metadata_ignore(bdb, table, column_name):
    metadata = bayesdb_metadata_categorical(bdb, table, column_name)
    metadata["modeltype"] = "ignore"
    return metadata

def bayesdb_metadata_key(bdb, table, column_name):
    metadata = bayesdb_metadata_categorical(bdb, table, column_name)
    metadata["modeltype"] = "key"
    return metadata

def bayesdb_metadata_categorical(bdb, table, column_name):
    qcn = sqlite3_quote_name(column_name)
    qt = sqlite3_quote_name(table)
    sql = """
        SELECT DISTINCT %s FROM %s WHERE %s IS NOT NULL ORDER BY %s
    """ % (qcn, qt, qcn, qcn)
    cursor = bdb.sqlite.execute(sql)
    codes = [row[0] for row in cursor]
    ncodes = len(codes)
    return {
        "modeltype": "symmetric_dirichlet_discrete",
        "value_to_code": dict(zip(range(ncodes), codes)),
        "code_to_value": dict(zip(codes, range(ncodes))),
    }

metadata_generators = {
    "numerical": bayesdb_metadata_numerical,
    "cyclic": bayesdb_metadata_cyclic,
    "ignore": bayesdb_metadata_ignore,   # XXX Why any metadata here?
    "key": bayesdb_metadata_categorical, # XXX Why any metadata here?
    "categorical": bayesdb_metadata_categorical,
}

### BayesDB data/metadata access

def bayesdb_data(bdb, table_id):
    M_c = bayesdb_metadata(bdb, table_id)
    table = bayesdb_table_name(bdb, table_id)
    column_names = list(bayesdb_column_names(bdb, table_id))
    qt = sqlite3_quote_name(table)
    qcns = ",".join(map(sqlite3_quote_name, column_names))
    sql = "SELECT %s FROM %s" % (qcns, qt)
    for row in bdb.sqlite.execute(sql):
        yield tuple(bayesdb_value_to_code(M_c, i, v)
            for i, v in enumerate(row))

def bayesdb_metadata(bdb, table_id):
    if bdb.metadata_cache is not None:
        if table_id in bdb.metadata_cache:
            return bdb.metadata_cache[table_id]
    sql = "SELECT metadata FROM bayesdb_table WHERE id = ?"
    metadata_json = sqlite3_exec_1(bdb.sqlite, sql, (table_id,))
    metadata = json.loads(metadata_json)
    if bdb.metadata_cache is not None:
        assert table_id not in bdb.metadata_cache
        bdb.metadata_cache[table_id] = metadata
    return metadata

def bayesdb_table_exists(bdb, table_name):
    sql = "SELECT COUNT(*) FROM bayesdb_table WHERE name = ?"
    return 0 < sqlite3_exec_1(bdb.sqlite, sql, (table_name,))

def bayesdb_table_name(bdb, table_id):
    sql = "SELECT name FROM bayesdb_table WHERE id = ?"
    return sqlite3_exec_1(bdb.sqlite, sql, (table_id,))

def bayesdb_table_id(bdb, table_name):
    sql = "SELECT id FROM bayesdb_table WHERE name = ?"
    return sqlite3_exec_1(bdb.sqlite, sql, (table_name,))

def bayesdb_column_names(bdb, table_id):
    sql = """
        SELECT name FROM bayesdb_table_column WHERE table_id = ? ORDER BY colno
    """
    for row in bdb.sqlite.execute(sql, (table_id,)):
        yield row[0]

def bayesdb_column_name(bdb, table_id, colno):
    sql = """
        SELECT name FROM bayesdb_table_column WHERE table_id = ? AND colno = ?
    """
    return sqlite3_exec_1(bdb.sqlite, sql, (table_id, colno))

def bayesdb_column_numbers(bdb, table_id):
    sql = """
        SELECT colno FROM bayesdb_table_column WHERE table_id = ?
        ORDER BY colno
    """
    for row in bdb.sqlite.execute(sql, (table_id,)):
        yield row[0]

def bayesdb_column_number(bdb, table_id, column_name):
    sql = """
        SELECT colno FROM bayesdb_table_column WHERE table_id = ? AND name = ?
    """
    return sqlite3_exec_1(bdb.sqlite, sql, (table_id, column_name))

def bayesdb_column_values(bdb, table_id, colno):
    qt = sqlite3_quote_name(bayesdb_table_name(bdb, table_id))
    qc = sqlite3_quote_name(bayesdb_column_name(bdb, table_id, colno))
    for row in bdb.sqlite.execute("SELECT %s FROM %s" % (qc, qt)):
        yield row[0]

def bayesdb_cell_value(bdb, table_id, rowid, colno):
    qt = sqlite3_quote_name(bayesdb_table_name(bdb, table_id))
    qc = sqlite3_quote_name(bayesdb_column_name(bdb, table_id, colno))
    sql = "SELECT %s FROM %s WHERE rowid = ?" % (qc, qt)
    return sqlite3_exec_1(bdb.sqlite, sql, (rowid,))

### BayesDB model access

def bayesdb_init_model(bdb, table_id, modelno, engine_id, theta,
        ifnotexists=False):
    insert_sql = """
        INSERT %s INTO bayesdb_model (table_id, modelno, engine_id, theta)
        VALUES (?, ?, ?, ?)
    """ % ("OR IGNORE" if ifnotexists else "")
    theta_json = json.dumps(theta)
    bdb.sqlite.execute(insert_sql, (table_id, modelno, engine_id, theta_json))
    if bdb.models_cache is not None:
        key = (table_id, modelno)
        if ifnotexists:
            if key not in bdb.models_cache:
                bdb.models_cache[key] = theta
        else:
            assert key not in bdb.models_cache
            bdb.models_cache[key] = theta

def bayesdb_set_model(bdb, table_id, modelno, theta):
    sql = """
        UPDATE bayesdb_model SET theta = ? WHERE table_id = ? AND modelno = ?
    """
    theta_json = json.dumps(theta)
    bdb.sqlite.execute(sql, (theta_json, table_id, modelno))
    if bdb.models_cache is not None:
        bdb.models_cache[table_id, modelno] = theta

def bayesdb_has_model(bdb, table_id, modelno):
    if bdb.models_cache is not None:
        if (table_id, modelno) in bdb.models_cache:
            return True
    sql = """
        SELECT count(*) FROM bayesdb_model WHERE table_id = ? AND modelno = ?
    """
    return 0 < sqlite3_exec_1(bdb.sqlite, sql, (table_id, modelno))

def bayesdb_nmodels(bdb, table_id):
    sql = """
        SELECT count(*) FROM bayesdb_model WHERE table_id = ?
    """
    return sqlite3_exec_1(bdb.sqlite, sql, (table_id,))

def bayesdb_models(bdb, table_id):
    for modelno in range(bayesdb_nmodels(bdb, table_id)):
        yield bayesdb_model(bdb, table_id, modelno)

def bayesdb_model(bdb, table_id, modelno):
    if bdb.models_cache is not None:
        key = (table_id, modelno)
        if key in bdb.models_cache:
            return bdb.models_cache[key]
    sql = """
        SELECT theta FROM bayesdb_model WHERE table_id = ? AND modelno = ?
    """
    theta_json = sqlite3_exec_1(bdb.sqlite, sql, (table_id, modelno))
    theta = json.loads(theta_json)
    if bdb.models_cache is not None:
        key = (table_id, modelno)
        assert key not in bdb.models_cache
        bdb.models_cache[key] = theta
    return theta

# XXX Silly name.
def bayesdb_latent_stata(bdb, table_id):
    for model in bayesdb_models(bdb, table_id):
        yield (model["X_L"], model["X_D"])

def bayesdb_latent_state(bdb, table_id):
    for model in bayesdb_models(bdb, table_id):
        yield model["X_L"]

def bayesdb_latent_data(bdb, table_id):
    for model in bayesdb_models(bdb, table_id):
        yield model["X_D"]

### BayesDB model commands

def bayesdb_models_initialize(bdb, table_id, nmodels, model_config=None,
        ifnotexists=False):
    if ifnotexists:
        # Find whether all model numbers are filled.  If so, don't
        # bother with initialization.
        #
        # XXX Are the models dependent on one another, or can we just
        # ask the engine to initialize as many models as we don't have
        # and fill in the gaps?
        done = True
        for modelno in range(nmodels):
            if not bayesdb_has_model(bdb, table_id, modelno):
                done = False
                break
        if done:
            return
    assert model_config is None         # XXX For now.
    assert 0 < nmodels
    engine_sql = "SELECT id FROM bayesdb_engine WHERE name = ?"
    engine_id = sqlite3_exec_1(bdb.sqlite, engine_sql, ("crosscat",)) # XXX
    model_config = {
        "kernel_list": (),
        "initialization": "from_the_prior",
        "row_initialization": "from_the_prior",
    }
    X_L_list, X_D_list = bdb.engine.initialize(
        M_c=bayesdb_metadata(bdb, table_id),
        M_r=None,            # XXX
        T=list(bayesdb_data(bdb, table_id)),
        n_chains=nmodels,
        initialization=model_config["initialization"],
        row_initialization=model_config["row_initialization"]
    )
    if nmodels == 1:            # XXX Ugh.  Fix crosscat so it doesn't do this.
        X_L_list = [X_L_list]
        X_D_list = [X_D_list]
    with bdb.savepoint():
        for modelno, (X_L, X_D) in enumerate(zip(X_L_list, X_D_list)):
            theta = {
                "X_L": X_L,
                "X_D": X_D,
                "iterations": 0,
                "column_crp_alpha": [],
                "logscore": [],
                "num_views": [],
                "model_config": model_config,
            }
            bayesdb_init_model(bdb, table_id, modelno, engine_id, theta,
                ifnotexists=ifnotexists)

# XXX Background, deadline, &c.
def bayesdb_models_analyze(bdb, table_id, iterations=1):
    for modelno in range(bayesdb_nmodels(bdb, table_id)):
        bayesdb_models_analyze1(bdb, table_id, modelno, iterations=iterations)

def bayesdb_models_analyze1(bdb, table_id, modelno, iterations=1):
    assert 0 <= iterations
    theta = bayesdb_model(bdb, table_id, modelno)
    if iterations < 1:
        return
    X_L, X_D, diagnostics = bdb.engine.analyze(
        M_c=bayesdb_metadata(bdb, table_id),
        T=list(bayesdb_data(bdb, table_id)),
        do_diagnostics=True,
        kernel_list=theta["model_config"]["kernel_list"],
        X_L=theta["X_L"],
        X_D=theta["X_D"],
        n_steps=iterations
    )
    theta["iterations"] += iterations
    theta["X_L"] = X_L
    theta["X_D"] = X_D
    # XXX Cargo-culted from old persistence layer's update_model.
    for diag_key in "column_crp_alpha", "logscore", "num_views":
        diag_list = [l[0] for l in diagnostics[diag_key]]
        if diag_key in theta and type(theta[diag_key]) == list:
            theta[diag_key] += diag_list
        else:
            theta[diag_key] = diag_list
    bayesdb_set_model(bdb, table_id, modelno, theta)

### BayesDB column functions

# Two-column function:  CORRELATION [OF <col0> WITH <col1>]
def bql_column_correlation(bdb, table_id, colno0, colno1):
    import scipy.stats          # pearsonr, chi2_contingency, f_oneway
    M_c = bayesdb_metadata(bdb, table_id)
    qt = sqlite3_quote_name(bayesdb_table_name(bdb, table_id))
    qc0 = sqlite3_quote_name(bayesdb_column_name(bdb, table_id, colno0))
    qc1 = sqlite3_quote_name(bayesdb_column_name(bdb, table_id, colno1))
    data0 = []
    data1 = []
    n = 0
    try:
        data_sql = """
            SELECT %s, %s FROM %s WHERE %s IS NOT NULL AND %s IS NOT NULL
        """ % (qc0, qc1, qt, qc0, qc1)
        for row in bdb.sqlite.execute(data_sql):
            data0.append(bayesdb_value_to_code(M_c, colno0, row[0]))
            data1.append(bayesdb_value_to_code(M_c, colno1, row[1]))
            n += 1
    except KeyError:
        # XXX Ugh!  What to do?  If we allow importing any SQL table
        # after its schema has been specified, we can't add a foreign
        # key constraint to that table's schema (unless we recreate
        # the table, which is no good for remote read-only tables).
        # We could deal exclusively in heuristic imports of CSV tables
        # and not bother to support importing SQL tables.
        return 0
    assert n == len(data0)
    assert n == len(data1)
    # XXX Push this into the engine.
    modeltype0 = M_c["column_metadata"][colno0]["modeltype"]
    modeltype1 = M_c["column_metadata"][colno1]["modeltype"]
    correlation = float("NaN")  # Default result.
    if bayesdb_modeltype_numerical_p(modeltype0) and \
       bayesdb_modeltype_numerical_p(modeltype1):
        # Both numerical: Pearson R^2
        sqrt_correlation, _p_value = scipy.stats.pearsonr(data0, data1)
        correlation = sqrt_correlation ** 2
    elif bayesdb_modeltype_discrete_p(modeltype0) and \
         bayesdb_modeltype_discrete_p(modeltype1):
        # Both categorical: Cramer's phi
        unique0 = unique_indices(data0)
        unique1 = unique_indices(data1)
        min_levels = min(len(unique0), len(unique1))
        if 1 < min_levels:
            ct = [0] * len(unique0)
            for i0, j0 in enumerate(unique0):
                ct[i0] = [0] * len(unique1)
                for i1, j1 in enumerate(unique1):
                    c = 0
                    for i in range(n):
                        if data0[i] == data0[j0] and data1[i] == data1[j1]:
                            c += 1
                    ct[i0][i1] = c
            chisq, _p, _dof, _expected = scipy.stats.chi2_contingency(ct,
                correction=False)
            correlation = math.sqrt(chisq / (n * (min_levels - 1)))
    else:
        # Numerical/categorical: ANOVA R^2
        if bayesdb_modeltype_discrete_p(modeltype0):
            assert bayesdb_modeltype_numerical_p(modeltype1)
            data_group = data0
            data_y = data1
        else:
            assert bayesdb_modeltype_numerical_p(modeltype0)
            assert bayesdb_modeltype_discrete_p(modeltype1)
            data_group = data1
            data_y = data0
        group_values = unique(data_group)
        n_groups = len(group_values)
        if n_groups < n:
            samples = []
            for v in group_values:
                sample = []
                for i in range(n):
                    if data_group[i] == v:
                        sample.append(data_y[i])
                samples.append(sample)
            F, _p = scipy.stats.f_oneway(*samples)
            correlation = 1 - 1/(1 + F*((n_groups - 1) / (n - n_groups)))
    return correlation

# Two-column function:  DEPENDENCE PROBABILITY [OF <col0> WITH <col1>]
def bql_column_dependence_probability(bdb, table_id, colno0, colno1):
    # XXX Push this into the engine.
    if colno0 == colno1:
        return 1
    count = 0
    nmodels = 0
    for X_L, X_D in bayesdb_latent_stata(bdb, table_id):
        nmodels += 1
        assignments = X_L["column_partition"]["assignments"]
        if assignments[colno0] != assignments[colno1]:
            continue
        if len(unique(X_D[assignments[colno0]])) <= 1:
            continue
        count += 1
    return float("NaN") if nmodels == 0 else (float(count) / float(nmodels))

# Two-column function:  MUTUAL INFORMATION [OF <col0> WITH <col1>]
def bql_column_mutual_information(bdb, table_id, colno0, colno1,
        numsamples=100):
    X_L_list = list(bayesdb_latent_state(bdb, table_id))
    X_D_list = list(bayesdb_latent_data(bdb, table_id))
    r = bdb.engine.mutual_information(
        M_c=bayesdb_metadata(bdb, table_id),
        X_L_list=X_L_list,
        X_D_list=X_D_list,
        Q=[(colno0, colno1)],
        n_samples=int(math.ceil(float(numsamples) / len(X_L_list)))
    )
    # r has one answer per element of Q, so take the first one.
    r0 = r[0]
    # r0 is (mi, linfoot), and we want mi.
    mi = r0[0]
    # mi is [result for model 0, result for model 1, ...], and we want
    # the mean.
    return arithmetic_mean(mi)

# One-column function:  TYPICALITY OF <col>
def bql_column_typicality(bdb, table_id, colno):
    return bdb.engine.column_structural_typicality(
        X_L_list=list(bayesdb_latent_state(bdb, table_id)),
        col_id=colno
    )

# One-column function:  PROBABILITY OF <col>=<value>
def bql_column_value_probability(bdb, table_id, colno, value):
    M_c = bayesdb_metadata(bdb, table_id)
    try:
        code = bayesdb_value_to_code(M_c, colno, value)
    except KeyError:
        return 0
    X_L_list = list(bayesdb_latent_state(bdb, table_id))
    X_D_list = list(bayesdb_latent_data(bdb, table_id))
    # Fabricate a nonexistent (`unobserved') row id.
    fake_row_id = len(X_D_list[0][0])
    r = bdb.engine.simple_predictive_probability_multistate(
        M_c=M_c,
        X_L_list=X_L_list,
        X_D_list=X_D_list,
        Y=[],
        Q=[(fake_row_id, colno, code)]
    )
    return math.exp(r)

### BayesDB row functions

# Row function:  SIMILARITY TO <target_row> [WITH RESPECT TO <columns>]
def bql_row_similarity(bdb, table_id, rowid, target_rowid, *columns):
    return bdb.engine.similarity(
        M_c=bayesdb_metadata(bdb, table_id),
        X_L_list=list(bayesdb_latent_state(bdb, table_id)),
        X_D_list=list(bayesdb_latent_data(bdb, table_id)),
        given_row_id=sqlite3_rowid_to_engine_row_id(rowid),
        target_row_id=sqlite3_rowid_to_engine_row_id(target_rowid),
        target_columns=list(columns)
    )

# Row function:  TYPICALITY
def bql_row_typicality(bdb, table_id, rowid):
    return bdb.engine.row_structural_typicality(
        X_L_list=list(bayesdb_latent_state(bdb, table_id)),
        X_D_list=list(bayesdb_latent_data(bdb, table_id)),
        row_id=sqlite3_rowid_to_engine_row_id(rowid)
    )

# Row function:  PREDICTIVE PROBABILITY OF <column>
def bql_row_column_predictive_probability(bdb, table_id, rowid, colno):
    M_c = bayesdb_metadata(bdb, table_id)
    value = bayesdb_cell_value(bdb, table_id, rowid, colno)
    code = bayesdb_value_to_code(M_c, colno, value)
    r = bdb.engine.simple_predictive_probability_multistate(
        M_c=M_c,
        X_L_list=list(bayesdb_latent_state(bdb, table_id)),
        X_D_list=list(bayesdb_latent_data(bdb, table_id)),
        Y=[],
        Q=[(sqlite3_rowid_to_engine_row_id(rowid), colno, code)]
    )
    return math.exp(r)

### Infer and simulate

def bql_infer(bdb, table_id, colno, rowid, value, confidence_threshold,
        numsamples=1):
    if value is not None:
        return value
    M_c = bayesdb_metadata(bdb, table_id)
    column_names = bayesdb_column_names(bdb, table_id)
    qt = sqlite3_quote_name(bayesdb_table_name(bdb, table_id))
    qcns = ",".join(map(sqlite3_quote_name, column_names))
    select_sql = "SELECT %s FROM %s WHERE rowid = ?" % (qcns, qt)
    c = bdb.sqlite.execute(select_sql, (rowid,))
    row = c.fetchone()
    assert row is not None
    assert c.fetchone() is None
    row_id = sqlite3_rowid_to_engine_row_id(rowid)
    code, confidence = bdb.engine.impute_and_confidence(
        M_c=M_c,
        X_L=list(bayesdb_latent_state(bdb, table_id)),
        X_D=list(bayesdb_latent_data(bdb, table_id)),
        Y=[(row_id, colno_, bayesdb_value_to_code(M_c, colno_, value))
            for colno_, value in enumerate(row) if value is not None],
        Q=[(row_id, colno)],
        n=numsamples
    )
    if confidence >= confidence_threshold:
        return bayesdb_code_to_value(M_c, colno, code)
    else:
        return None

# XXX Create a virtual table that simulates results?
def bayesdb_simulate(bdb, table_id, constraints, colnos, numpredictions=1):
    M_c = bayesdb_metadata(bdb, table_id)
    qt = sqlite3_quote_name(bayesdb_table_name(bdb, table_id))
    max_rowid = sqlite3_exec_1(bdb.sqlite, "SELECT max(rowid) FROM %s" % (qt,))
    fake_rowid = max_rowid + 1
    fake_row_id = sqlite3_rowid_to_engine_row_id(fake_rowid)
    # XXX Why special-case empty constraints?
    Y = None
    if constraints is not None:
        Y = [(fake_row_id, colno, bayesdb_value_to_code(M_c, colno, value))
             for colno, value in constraints]
    raw_outputs = bdb.engine.simple_predictive_sample(
        M_c=M_c,
        X_L=list(bayesdb_latent_state(bdb, table_id)),
        X_D=list(bayesdb_latent_data(bdb, table_id)),
        Y=Y,
        Q=[(fake_row_id, colno) for colno in colnos],
        n=numpredictions
    )
    return [[bayesdb_code_to_value(M_c, colno, code)
            for (colno, code) in zip(colnos, raw_output)]
        for raw_output in raw_outputs]

### BayesDB utilities

def bayesdb_value_to_code(M_c, colno, value):
    metadata = M_c["column_metadata"][colno]
    modeltype = metadata["modeltype"]
    if bayesdb_modeltype_discrete_p(modeltype):
        # For hysterical raisins, code_to_value and value_to_code are
        # backwards.
        #
        # XXX Fix this.
        if value is None:
            return float("NaN")         # XXX !?!??!
        # XXX Crosscat expects floating-point codes.
        return float(metadata["code_to_value"][unicode(value)])
    elif bayesdb_modeltype_numerical_p(modeltype):
        if value is None:
            return float("NaN")
        return float(value)
    else:
        raise KeyError

def bayesdb_code_to_value(M_c, colno, code):
    metadata = M_c["column_metadata"][colno]
    modeltype = metadata["modeltype"]
    if bayesdb_modeltype_discrete_p(modeltype):
        if math.isnan(code):
            return None
        # XXX Whattakludge.
        return metadata["value_to_code"][unicode(int(code))]
    elif bayesdb_modeltype_numerical_p(modeltype):
        if math.isnan(code):
            return None
        return code
    else:
        raise KeyError

bayesdb_modeltypes_discrete = \
    set(mt for _ct, cont_p, _sql, mt in bayesdb_type_table if not cont_p)
def bayesdb_modeltype_discrete_p(modeltype):
    return modeltype in bayesdb_modeltypes_discrete

bayesdb_modeltypes_numerical = \
    set(mt for _ct, cont_p, _sql, mt in bayesdb_type_table if cont_p)
def bayesdb_modeltype_numerical_p(modeltype):
    return modeltype in bayesdb_modeltypes_numerical

# By default, SQLite3 automatically numbers rows starting at 1, and
# not necessarily contiguously (although they are noncontiguous only
# if rows are deleted).  Crosscat expects contiguous 0-indexed row
# ids.  For now, we'll judiciously map between them, and maintain the
# convention that `rowid' means a SQLite3 rowid (like the ROWID column
# built-in to tables by default) and `row_id' means a Crosscat row id.
#
# XXX Revisit row numbering between SQLite3 and Crosscat.

def sqlite3_rowid_to_engine_row_id(rowid):
    return rowid - 1

def engine_row_id_to_sqlite3_rowid(row_id):
    return row_id + 1
