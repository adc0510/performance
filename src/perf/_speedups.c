/* perf._speedups -- C implementation of perf's synchronous node wrapper.
 *
 * NodeWrapper replaces the closure built by perf._sync_wrap. It does the
 * same work in the same order (see _sync_wrap in __init__.py), but in C:
 *
 *   - outside any trace (_current_node is None) the call is forwarded to the
 *     original function with vectorcall, with no Python frame in between;
 *   - inside a trace, the node bookkeeping (make_node, _recording, attach,
 *     _restart, _completion, _TraceState.complete, the ContextVar dance) runs
 *     here, while value capture, TRACE_LOCALS and exception recording call
 *     the existing Python helpers;
 *   - rare paths (SKIP_FUNCTIONS set, inside a skipped call, a pending
 *     TIMEOUT) are handed to the pure-Python wrapper (`slow`) unchanged.
 *
 * perf/__init__.py calls init(globals()) once, after every name used here
 * is defined. Python 3.12+ only (PyErr_GetRaisedException); older versions
 * use the pure-Python wrapper.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <structmember.h>

#if PY_VERSION_HEX < 0x030C0000
#error "perf._speedups needs Python 3.12+"
#endif

/* ── state taken from perf's module globals by init() ─────────────────────── */

static PyObject *g_globals;          /* perf's module dict (live config) */
static PyObject *g_current_node;     /* ContextVar */
static PyObject *g_skip_depth;       /* ContextVar */
static PyObject *g_capturing;        /* ContextVar */
static PyObject *g_current_trace;    /* ContextVar */
static PyObject *g_overrides;        /* ContextVar */
static PyObject *g_timeout_pending;  /* list */
static PyObject *g_record_inputs;    /* _record_inputs(node, args, kwargs, param_names) */
static PyObject *g_record_output;    /* _record_output(done, result) */
static PyObject *g_sync_failure;     /* _sync_failure(done, exc, suppress, default_factory) */
static PyObject *g_reraise;          /* sentinel returned by _sync_failure */
static PyObject *g_locals_wanted;    /* _locals_wanted(name) */
static PyObject *g_locals_enable;    /* _locals_enable(code, node) */
static PyObject *g_round;            /* builtins.round */
static PyObject *g_nine;             /* 9 == _TIME_DECIMALS */

static PyObject *s_name, *s_time, *s_start, *s_children, *s_capture_time, *s_capture_skipped;
static PyObject *s_lock, *s_open, *s_acquire, *s_release;
static PyObject *s_SKIP_FUNCTIONS, *s_CAPTURE_IO, *s_locals_users, *s_qualname;

static int g_ready = 0;

/* ── small helpers ────────────────────────────────────────────────────────── */

static double
now(void)
{
#if PY_VERSION_HEX >= 0x030D0000
    PyTime_t t;
    if (PyTime_PerfCounterRaw(&t) < 0) {
        return 0.0;
    }
    return PyTime_AsSecondsDouble(t);  /* bit-identical to time.perf_counter() */
#else
    static PyObject *perf_counter = NULL;
    if (perf_counter == NULL) {
        PyObject *time = PyImport_ImportModule("time");
        if (time == NULL) return 0.0;
        perf_counter = PyObject_GetAttrString(time, "perf_counter");
        Py_DECREF(time);
        if (perf_counter == NULL) return 0.0;
    }
    PyObject *r = PyObject_CallNoArgs(perf_counter);
    if (r == NULL) return 0.0;
    double d = PyFloat_AsDouble(r);
    Py_DECREF(r);
    return d;
#endif
}

/* round(value, 9) via the builtin, so results match the Python code exactly. */
static PyObject *
round9_obj(PyObject *value)
{
    PyObject *args[2] = {value, g_nine};
    return PyObject_Vectorcall(g_round, args, 2, NULL);
}

static PyObject *
round9(double d)
{
    PyObject *f = PyFloat_FromDouble(d);
    if (f == NULL) return NULL;
    PyObject *r = round9_obj(f);
    Py_DECREF(f);
    return r;
}

/* New reference to a ContextVar's current value (all of perf's have defaults). */
static PyObject *
ctx_get(PyObject *var)
{
    PyObject *value = NULL;
    if (PyContextVar_Get(var, NULL, &value) < 0) return NULL;
    if (value == NULL) {
        Py_RETURN_NONE;
    }
    return value;
}

/* Truthiness of a ContextVar's value: 1, 0, or -1 on error. */
static int
ctx_true(PyObject *var)
{
    PyObject *v = ctx_get(var);
    if (v == NULL) return -1;
    int r = PyObject_IsTrue(v);
    Py_DECREF(v);
    return r;
}

/* Truthiness of the effective config value `name` (what perf._cfg.<name>
 * returns): the context's override if any, else the module global. */
static int
cfg_true(PyObject *name)
{
    PyObject *overrides = ctx_get(g_overrides);
    if (overrides == NULL) return -1;
    if (overrides != Py_None && PyDict_Check(overrides)) {
        PyObject *v = PyDict_GetItemWithError(overrides, name);  /* borrowed */
        if (v != NULL) {
            Py_INCREF(v);
            Py_DECREF(overrides);
            int r = PyObject_IsTrue(v);
            Py_DECREF(v);
            return r;
        }
        if (PyErr_Occurred()) {
            Py_DECREF(overrides);
            return -1;
        }
    }
    Py_DECREF(overrides);
    PyObject *v = PyDict_GetItemWithError(g_globals, name);  /* borrowed */
    if (v == NULL) return PyErr_Occurred() ? -1 : 0;
    return PyObject_IsTrue(v);
}

/* state.lock.acquire() / release() */
static int
lock_call(PyObject *state, PyObject *method)
{
    PyObject *lock = PyObject_GetAttr(state, s_lock);
    if (lock == NULL) return -1;
    PyObject *r = PyObject_CallMethodNoArgs(lock, method);
    Py_DECREF(lock);
    if (r == NULL) return -1;
    Py_DECREF(r);
    return 0;
}

/* _TraceState.attach(parent, node): 1 attached, 0 trace closed, -1 error. */
static int
state_attach(PyObject *state, PyObject *parent, PyObject *node)
{
    if (lock_call(state, s_acquire) < 0) return -1;
    int result = -1;
    PyObject *open = PyObject_GetAttr(state, s_open);
    if (open != NULL) {
        int is_open = PyObject_IsTrue(open);
        Py_DECREF(open);
        if (is_open == 0) {
            result = 0;
        }
        else if (is_open > 0) {
            PyObject *children = PyDict_GetItemWithError(parent, s_children);  /* borrowed */
            if (children != NULL && PyList_Append(children, node) == 0) {
                result = 1;
            }
            else if (children == NULL && !PyErr_Occurred()) {
                PyErr_SetString(PyExc_KeyError, "children");
            }
        }
    }
    PyObject *exc = result < 0 ? PyErr_GetRaisedException() : NULL;
    int released = lock_call(state, s_release);
    if (exc != NULL) {
        if (released < 0) {
            PyObject *exc2 = PyErr_GetRaisedException();
            PyException_SetContext(exc2, exc);
            PyErr_SetRaisedException(exc2);
        }
        else {
            PyErr_SetRaisedException(exc);
        }
        return -1;
    }
    return released < 0 ? -1 : result;
}

/* _TraceState.complete(node, fields) */
static int
state_complete(PyObject *state, PyObject *node, PyObject *fields)
{
    PyObject *ct = PyDict_GetItemWithError(fields, s_capture_time);  /* borrowed */
    if (ct != NULL) {
        PyObject *rounded = round9_obj(ct);
        if (rounded == NULL) return -1;
        int r = PyDict_SetItem(fields, s_capture_time, rounded);
        Py_DECREF(rounded);
        if (r < 0) return -1;
    }
    else if (PyErr_Occurred()) {
        return -1;
    }
    if (lock_call(state, s_acquire) < 0) return -1;
    int result = 0;
    PyObject *open = PyObject_GetAttr(state, s_open);
    if (open == NULL) {
        result = -1;
    }
    else {
        int is_open = PyObject_IsTrue(open);
        Py_DECREF(open);
        if (is_open < 0 || (is_open > 0 && PyDict_Update(node, fields) < 0)) {
            result = -1;
        }
    }
    PyObject *exc = result < 0 ? PyErr_GetRaisedException() : NULL;
    int released = lock_call(state, s_release);
    if (exc != NULL) {
        PyErr_SetRaisedException(exc);
        return -1;
    }
    return released;
}

/* make_node(name) */
static PyObject *
make_node(PyObject *name)
{
    PyObject *node = PyDict_New();
    if (node == NULL) return NULL;
    PyObject *start = PyFloat_FromDouble(now());
    PyObject *children = PyList_New(0);
    if (start == NULL || children == NULL
        || PyDict_SetItem(node, s_name, name) < 0
        || PyDict_SetItem(node, s_time, Py_None) < 0
        || PyDict_SetItem(node, s_start, start) < 0
        || PyDict_SetItem(node, s_children, children) < 0) {
        Py_XDECREF(start);
        Py_XDECREF(children);
        Py_DECREF(node);
        return NULL;
    }
    Py_DECREF(start);
    Py_DECREF(children);
    return node;
}

/* _restart(node) */
static int
restart(PyObject *node)
{
    PyObject *start = round9(now());
    if (start == NULL) return -1;
    int r = PyDict_SetItem(node, s_start, start);
    Py_DECREF(start);
    return r;
}

/* _completion(node) */
static PyObject *
completion(PyObject *node)
{
    double end = now();
    PyObject *start = PyDict_GetItemWithError(node, s_start);  /* borrowed */
    if (start == NULL) {
        if (!PyErr_Occurred()) PyErr_SetString(PyExc_KeyError, "start");
        return NULL;
    }
    double s = PyFloat_AsDouble(start);
    if (s == -1.0 && PyErr_Occurred()) return NULL;
    PyObject *fields = PyDict_New();
    if (fields == NULL) return NULL;
    PyObject *time = round9(end - s);
    if (time == NULL || PyDict_SetItem(fields, s_time, time) < 0) {
        Py_XDECREF(time);
        Py_DECREF(fields);
        return NULL;
    }
    Py_DECREF(time);
    PyObject *ct = PyDict_GetItemWithError(node, s_capture_time);  /* borrowed */
    if ((ct != NULL && PyDict_SetItem(fields, s_capture_time, ct) < 0) || (ct == NULL && PyErr_Occurred())) {
        Py_DECREF(fields);
        return NULL;
    }
    /* MAX_CAPTURES_PER_FUNCTION: so _record_output skips this call too */
    int skipped = PyDict_Contains(node, s_capture_skipped);
    if (skipped < 0 || (skipped && PyDict_SetItem(fields, s_capture_skipped, Py_True) < 0)) {
        Py_DECREF(fields);
        return NULL;
    }
    return fields;
}

/* Positional args as a tuple and keyword args as a dict, from a vectorcall. */
static int
unpack_call(PyObject *const *args, Py_ssize_t nargs, PyObject *kwnames, PyObject **pargs, PyObject **pkwargs)
{
    *pargs = PyTuple_New(nargs);
    *pkwargs = PyDict_New();
    if (*pargs == NULL || *pkwargs == NULL) goto error;
    for (Py_ssize_t i = 0; i < nargs; i++) {
        Py_INCREF(args[i]);
        PyTuple_SET_ITEM(*pargs, i, args[i]);
    }
    if (kwnames != NULL) {
        Py_ssize_t nkw = PyTuple_GET_SIZE(kwnames);
        for (Py_ssize_t i = 0; i < nkw; i++) {
            if (PyDict_SetItem(*pkwargs, PyTuple_GET_ITEM(kwnames, i), args[nargs + i]) < 0) goto error;
        }
    }
    return 0;
error:
    Py_CLEAR(*pargs);
    Py_CLEAR(*pkwargs);
    return -1;
}

/* Replace the current exception with `exc2`, chaining the old one as its
 * __context__ (what Python does for an exception raised in an except/finally). */
static void
chain_onto(PyObject *exc)
{
    PyObject *exc2 = PyErr_GetRaisedException();
    if (exc != NULL) {
        PyException_SetContext(exc2, exc);  /* steals exc */
    }
    PyErr_SetRaisedException(exc2);
}

/* ── NodeWrapper ──────────────────────────────────────────────────────────── */

typedef struct {
    PyObject_HEAD
    PyObject *func;
    PyObject *name;
    PyObject *slow;             /* the pure-Python wrapper, for rare paths */
    PyObject *extra;            /* None or callable (see _cache_wrap) */
    PyObject *suppress;
    PyObject *default_factory;
    PyObject *param_names;
    PyObject *inner_code;
    PyObject *dict;
    PyObject *weakreflist;
    vectorcallfunc vectorcall;
} NodeWrapper;

static PyObject *
nw_passthrough(NodeWrapper *self, PyObject *const *args, size_t nargsf, PyObject *kwnames)
{
    return PyObject_Vectorcall(self->func, args, nargsf, kwnames);
}

static PyObject *
nw_vectorcall(PyObject *op, PyObject *const *args, size_t nargsf, PyObject *kwnames)
{
    NodeWrapper *self = (NodeWrapper *)op;
    if (!g_ready) {
        return PyObject_Vectorcall(self->slow, args, nargsf, kwnames);
    }
    PyObject *parent = ctx_get(g_current_node);
    if (parent == NULL) return NULL;
    if (parent == Py_None) {
        /* Not inside any trace: behave exactly like the original. */
        Py_DECREF(parent);
        return nw_passthrough(self, args, nargsf, kwnames);
    }

    /* Rare paths: let the Python wrapper handle them exactly as before. */
    int skip = cfg_true(s_SKIP_FUNCTIONS);
    int depth = skip ? 0 : ctx_true(g_skip_depth);
    if (skip < 0 || depth < 0) goto fail_parent;
    if (skip || depth || PyList_GET_SIZE(g_timeout_pending) > 0) {
        Py_DECREF(parent);
        return PyObject_Vectorcall(self->slow, args, nargsf, kwnames);
    }

    /* _recording() */
    int capturing = ctx_true(g_capturing);
    if (capturing < 0) goto fail_parent;
    if (capturing) {
        Py_DECREF(parent);
        return nw_passthrough(self, args, nargsf, kwnames);
    }
    PyObject *state = ctx_get(g_current_trace);
    if (state == NULL) goto fail_parent;
    int is_open = 0;
    if (state != Py_None) {
        PyObject *open = PyObject_GetAttr(state, s_open);
        if (open == NULL) goto fail_state;
        is_open = PyObject_IsTrue(open);
        Py_DECREF(open);
        if (is_open < 0) goto fail_state;
    }
    if (!is_open) {
        Py_DECREF(state);
        Py_DECREF(parent);
        return nw_passthrough(self, args, nargsf, kwnames);
    }

    PyObject *node = make_node(self->name);
    if (node == NULL) goto fail_state;

    /* Inputs are snapshotted BEFORE the call (see _sync_wrap). */
    Py_ssize_t nargs = PyVectorcall_NARGS(nargsf);
    int capture_io = cfg_true(s_CAPTURE_IO);
    if (capture_io < 0) goto fail_node;
    if (capture_io) {
        PyObject *targs, *tkwargs;
        if (unpack_call(args, nargs, kwnames, &targs, &tkwargs) < 0) goto fail_node;
        PyObject *cargs[4] = {node, targs, tkwargs, self->param_names};
        PyObject *r = PyObject_Vectorcall(g_record_inputs, cargs, 4, NULL);
        Py_DECREF(targs);
        Py_DECREF(tkwargs);
        if (r == NULL) goto fail_node;
        Py_DECREF(r);
    }
    int attached = state_attach(state, parent, node);
    if (attached < 0) goto fail_node;
    if (!attached) {  /* the trace closed meanwhile */
        Py_DECREF(node);
        Py_DECREF(state);
        Py_DECREF(parent);
        return nw_passthrough(self, args, nargsf, kwnames);
    }
    Py_DECREF(parent);
    parent = NULL;

    /* TRACE_LOCALS */
    PyObject *locals_users = PyDict_GetItemWithError(g_globals, s_locals_users);  /* borrowed */
    if (locals_users == NULL && PyErr_Occurred()) goto fail_node;
    if (locals_users != NULL) {
        int lu = PyObject_IsTrue(locals_users);
        if (lu < 0) goto fail_node;
        if (lu) {
            PyObject *wanted = PyObject_CallOneArg(g_locals_wanted, self->name);
            if (wanted == NULL) goto fail_node;
            int w = PyObject_IsTrue(wanted);
            Py_DECREF(wanted);
            if (w < 0) goto fail_node;
            if (w) {
                PyObject *largs[2] = {self->inner_code, node};
                PyObject *r = PyObject_Vectorcall(g_locals_enable, largs, 2, NULL);
                if (r == NULL) goto fail_node;
                Py_DECREF(r);
            }
        }
    }

    PyObject *token = PyContextVar_Set(g_current_node, node);
    if (token == NULL) goto fail_node;
    PyObject *finish_extra = NULL;
    PyObject *done = NULL;
    PyObject *result = NULL;
    PyObject *pending = NULL;  /* exception being propagated, while `finally` runs */

    if (self->extra != Py_None) {
        finish_extra = PyObject_CallNoArgs(self->extra);
        if (finish_extra == NULL) goto finally;  /* raised before the `try` in Python too */
    }
    if (restart(node) < 0) goto finally;

    /* try: */
    result = PyObject_Vectorcall(self->func, args, nargsf, kwnames);
    if (result != NULL) {
        done = completion(node);
        if (done != NULL) {
            int cap = cfg_true(s_CAPTURE_IO);
            if (cap > 0) {
                PyObject *oargs[2] = {done, result};
                PyObject *r = PyObject_Vectorcall(g_record_output, oargs, 2, NULL);
                if (r == NULL) {
                    Py_CLEAR(result);
                }
                Py_XDECREF(r);
            }
            else if (cap < 0) {
                Py_CLEAR(result);
            }
        }
        else {
            Py_CLEAR(result);
        }
    }
    if (result == NULL) {
        /* except BaseException as exc: */
        PyObject *exc = PyErr_GetRaisedException();
        Py_XDECREF(done);
        done = completion(node);
        if (done == NULL) {
            chain_onto(exc);
            goto finally;
        }
        PyObject *fargs[4] = {done, exc, self->suppress, self->default_factory};
        PyObject *r = PyObject_Vectorcall(g_sync_failure, fargs, 4, NULL);
        if (r == NULL) {
            chain_onto(exc);
        }
        else if (r == g_reraise) {
            Py_DECREF(r);
            PyErr_SetRaisedException(exc);
        }
        else {
            Py_DECREF(exc);
            result = r;  /* SUPPRESS_ERRORS: the default return value */
        }
    }

finally:
    pending = result == NULL ? PyErr_GetRaisedException() : NULL;
    if (done != NULL) {
        int failed = 0;
        if (finish_extra != NULL) {
            PyObject *fields = PyObject_CallNoArgs(finish_extra);
            if (fields == NULL || PyDict_Update(done, fields) < 0) failed = 1;
            Py_XDECREF(fields);
        }
        if (!failed && state_complete(state, node, done) < 0) failed = 1;
        if (failed) {
            Py_CLEAR(result);
            chain_onto(pending);
            pending = NULL;
        }
    }
    if (PyContextVar_Reset(g_current_node, token) < 0) {
        Py_CLEAR(result);
        chain_onto(pending);
        pending = NULL;
    }
    if (pending != NULL) {
        PyErr_SetRaisedException(pending);
    }
    Py_DECREF(token);
    Py_XDECREF(finish_extra);
    Py_XDECREF(done);
    Py_DECREF(node);
    Py_DECREF(state);
    return result;

fail_node:
    Py_DECREF(node);
fail_state:
    Py_DECREF(state);
fail_parent:
    Py_XDECREF(parent);
    return NULL;
}

static PyObject *
nw_new(PyTypeObject *type, PyObject *args, PyObject *kwds)
{
    PyObject *func, *name, *slow, *extra, *suppress, *default_factory, *param_names, *inner_code;
    if (kwds != NULL && PyDict_GET_SIZE(kwds)) {
        PyErr_SetString(PyExc_TypeError, "NodeWrapper takes positional arguments only");
        return NULL;
    }
    if (!PyArg_UnpackTuple(args, "NodeWrapper", 8, 8, &func, &name, &slow, &extra, &suppress,
                           &default_factory, &param_names, &inner_code)) {
        return NULL;
    }
    NodeWrapper *self = (NodeWrapper *)type->tp_alloc(type, 0);
    if (self == NULL) return NULL;
    self->func = Py_NewRef(func);
    self->name = Py_NewRef(name);
    self->slow = Py_NewRef(slow);
    self->extra = Py_NewRef(extra);
    self->suppress = Py_NewRef(suppress);
    self->default_factory = Py_NewRef(default_factory);
    self->param_names = Py_NewRef(param_names);
    self->inner_code = Py_NewRef(inner_code);
    self->dict = NULL;
    self->weakreflist = NULL;
    self->vectorcall = nw_vectorcall;
    return (PyObject *)self;
}

static int
nw_traverse(NodeWrapper *self, visitproc visit, void *arg)
{
    Py_VISIT(Py_TYPE(self));
    Py_VISIT(self->func);
    Py_VISIT(self->name);
    Py_VISIT(self->slow);
    Py_VISIT(self->extra);
    Py_VISIT(self->suppress);
    Py_VISIT(self->default_factory);
    Py_VISIT(self->param_names);
    Py_VISIT(self->inner_code);
    Py_VISIT(self->dict);
    return 0;
}

static int
nw_clear(NodeWrapper *self)
{
    Py_CLEAR(self->func);
    Py_CLEAR(self->name);
    Py_CLEAR(self->slow);
    Py_CLEAR(self->extra);
    Py_CLEAR(self->suppress);
    Py_CLEAR(self->default_factory);
    Py_CLEAR(self->param_names);
    Py_CLEAR(self->inner_code);
    Py_CLEAR(self->dict);
    return 0;
}

static void
nw_dealloc(NodeWrapper *self)
{
    PyObject_GC_UnTrack(self);
    if (self->weakreflist != NULL) PyObject_ClearWeakRefs((PyObject *)self);
    nw_clear(self);
    PyTypeObject *tp = Py_TYPE(self);
    tp->tp_free((PyObject *)self);
    Py_DECREF(tp);
}

/* Bind like a function does, so wrapped methods work on classes. */
static PyObject *
nw_descr_get(PyObject *self, PyObject *obj, PyObject *type)
{
    if (obj == NULL || obj == Py_None) return Py_NewRef(self);
    return PyMethod_New(self, obj);
}

static PyObject *
nw_repr(NodeWrapper *self)
{
    PyObject *qualname = self->dict ? PyDict_GetItemWithError(self->dict, s_qualname) : NULL;
    if (qualname == NULL && PyErr_Occurred()) return NULL;
    if (qualname == NULL) qualname = self->name;
    return PyUnicode_FromFormat("<function %S at %p>", qualname, self);
}

/* Pickle by reference, like a function: `module.qualname`. */
static PyObject *
nw_reduce(NodeWrapper *self, PyObject *Py_UNUSED(ignored))
{
    return PyObject_GetAttr((PyObject *)self, s_qualname);
}

static PyMethodDef nw_methods[] = {
    {"__reduce__", (PyCFunction)nw_reduce, METH_NOARGS, NULL},
    {NULL}
};

static PyMemberDef nw_members[] = {
    {"_perf_slow", Py_T_OBJECT_EX, offsetof(NodeWrapper, slow), Py_READONLY, NULL},
    {"__vectorcalloffset__", Py_T_PYSSIZET, offsetof(NodeWrapper, vectorcall), Py_READONLY, NULL},
    {"__dictoffset__", Py_T_PYSSIZET, offsetof(NodeWrapper, dict), Py_READONLY, NULL},
    {"__weaklistoffset__", Py_T_PYSSIZET, offsetof(NodeWrapper, weakreflist), Py_READONLY, NULL},
    {NULL}
};

static PyGetSetDef nw_getset[] = {
    {"__dict__", PyObject_GenericGetDict, PyObject_GenericSetDict, NULL, NULL},
    {NULL}
};

static PyType_Slot nw_slots[] = {
    {Py_tp_new, nw_new},
    {Py_tp_dealloc, nw_dealloc},
    {Py_tp_traverse, nw_traverse},
    {Py_tp_clear, nw_clear},
    {Py_tp_call, PyVectorcall_Call},
    {Py_tp_descr_get, nw_descr_get},
    {Py_tp_repr, nw_repr},
    {Py_tp_methods, nw_methods},
    {Py_tp_members, nw_members},
    {Py_tp_getset, nw_getset},
    {0, NULL}
};

static PyType_Spec nw_spec = {
    .name = "perf._speedups.NodeWrapper",
    .basicsize = sizeof(NodeWrapper),
    .flags = Py_TPFLAGS_DEFAULT | Py_TPFLAGS_HAVE_GC | Py_TPFLAGS_HAVE_VECTORCALL | Py_TPFLAGS_METHOD_DESCRIPTOR,
    .slots = nw_slots,
};

/* ── module ───────────────────────────────────────────────────────────────── */

static PyObject *
take(PyObject *globals, const char *name)
{
    PyObject *v = PyDict_GetItemString(globals, name);  /* borrowed */
    if (v == NULL) {
        PyErr_Format(PyExc_RuntimeError, "perf._speedups.init: perf has no %s", name);
        return NULL;
    }
    return Py_NewRef(v);
}

static PyObject *
sp_init(PyObject *module, PyObject *globals)
{
    if (!PyDict_Check(globals)) {
        PyErr_SetString(PyExc_TypeError, "init() expects perf's globals() dict");
        return NULL;
    }
    if (g_ready) Py_RETURN_NONE;
    g_globals = Py_NewRef(globals);
    if (!(g_current_node = take(globals, "_current_node"))
        || !(g_skip_depth = take(globals, "_skip_depth"))
        || !(g_capturing = take(globals, "_capturing"))
        || !(g_current_trace = take(globals, "_current_trace"))
        || !(g_overrides = take(globals, "_overrides"))
        || !(g_timeout_pending = take(globals, "_timeout_pending"))
        || !(g_record_inputs = take(globals, "_record_inputs"))
        || !(g_record_output = take(globals, "_record_output"))
        || !(g_sync_failure = take(globals, "_sync_failure"))
        || !(g_reraise = take(globals, "_RERAISE"))
        || !(g_locals_wanted = take(globals, "_locals_wanted"))
        || !(g_locals_enable = take(globals, "_locals_enable"))) {
        return NULL;
    }
    if (!PyList_Check(g_timeout_pending)) {
        PyErr_SetString(PyExc_TypeError, "perf._timeout_pending must be a list");
        return NULL;
    }
    PyObject *builtins = PyEval_GetBuiltins();  /* borrowed */
    g_round = Py_NewRef(PyDict_GetItemString(builtins, "round"));
    g_nine = PyLong_FromLong(9);
    if (g_nine == NULL) return NULL;
    g_ready = 1;
    Py_RETURN_NONE;
}

static PyMethodDef sp_methods[] = {
    {"init", sp_init, METH_O, "Bind to perf's module globals (called once by perf)."},
    {NULL}
};

static int
sp_exec(PyObject *module)
{
#define INTERN(var, text) if (!(var = PyUnicode_InternFromString(text))) return -1;
    INTERN(s_name, "name");
    INTERN(s_time, "time");
    INTERN(s_start, "start");
    INTERN(s_children, "children");
    INTERN(s_capture_time, "capture_time");
    INTERN(s_capture_skipped, "capture_skipped");
    INTERN(s_lock, "lock");
    INTERN(s_open, "open");
    INTERN(s_acquire, "acquire");
    INTERN(s_release, "release");
    INTERN(s_SKIP_FUNCTIONS, "SKIP_FUNCTIONS");
    INTERN(s_CAPTURE_IO, "CAPTURE_IO");
    INTERN(s_locals_users, "_locals_users");
    INTERN(s_qualname, "__qualname__");
#undef INTERN
    PyObject *type = PyType_FromModuleAndSpec(module, &nw_spec, NULL);
    if (type == NULL) return -1;
    if (PyModule_AddObjectRef(module, "NodeWrapper", type) < 0) {
        Py_DECREF(type);
        return -1;
    }
    Py_DECREF(type);
    return 0;
}

static PyModuleDef_Slot sp_slots[] = {
    {Py_mod_exec, sp_exec},
    {0, NULL}
};

static struct PyModuleDef sp_module = {
    PyModuleDef_HEAD_INIT,
    .m_name = "perf._speedups",
    .m_doc = "C implementation of perf's synchronous node wrapper.",
    .m_size = 0,
    .m_methods = sp_methods,
    .m_slots = sp_slots,
};

PyMODINIT_FUNC
PyInit__speedups(void)
{
    return PyModuleDef_Init(&sp_module);
}
