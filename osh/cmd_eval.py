#!/usr/bin/env python2
# Copyright 2016 Andy Chu. All rights reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
"""
cmd_eval.py -- Interpreter for the command language.

Problems:
$ < Makefile cat | < NOTES.txt head

This just does head?  Last one wins.
"""
from __future__ import print_function

import sys

from _devbuild.gen.id_kind_asdl import Id, Id_str
from _devbuild.gen.option_asdl import option_i
from _devbuild.gen.syntax_asdl import (
    compound_word,
    command_e, command_t,
    command__AndOr, command__Case, command__CommandList, command__ControlFlow,
    command__DBracket, command__DoGroup, command__DParen,
    command__ExpandedAlias, command__Expr, command__ForEach, command__ForExpr,
    command__Func, command__If, command__NoOp, command__OilForIn,
    command__Pipeline, command__PlaceMutation, command__Proc,
    command__Sentence, command__ShAssignment, command__ShFunction,
    command__Simple, command__Subshell, command__TimeBlock, command__VarDecl,
    command__WhileUntil,
    condition_e, condition_t, condition__Shell, condition__Oil,
    BraceGroup, expr__BlockArg, ArgList,
    assign_op_e,
    place_expr__Var,
    proc_sig_e, proc_sig__Closed,
    redir_param_e, redir_param__HereDoc, proc_sig
)
from _devbuild.gen.runtime_asdl import (
    lvalue, lvalue_e, lvalue__ObjIndex, lvalue__ObjAttr,
    value, value_e, value_t, value__Str, value__MaybeStrArray,
    redirect, redirect_arg, scope_e,
    cmd_value_e, cmd_value__Argv, cmd_value__Assign,
    CompoundStatus, Proc
)
from _devbuild.gen.types_asdl import redir_arg_type_e

from asdl import runtime
from core import dev
from core import error
from core.error import _ControlFlow
from core.pyerror import log, e_die
from core import pyos  # Time().  TODO: rename
from core import state
from core import ui
from core import util
from core import vm
from frontend import consts
from frontend import location
from oil_lang import objects
from osh import braces
from osh import sh_expr_eval
from osh import word_
from osh import word_eval
from mycpp import mylib
from mycpp.mylib import switch, tagswitch

import posix_ as posix
import libc  # for fnmatch

from typing import List, Dict, Tuple, Any, cast, TYPE_CHECKING

if TYPE_CHECKING:
  from _devbuild.gen.id_kind_asdl import Id_t
  from _devbuild.gen.option_asdl import builtin_t
  from _devbuild.gen.runtime_asdl import (
      cmd_value_t, cell, lvalue_t,
  )
  from _devbuild.gen.syntax_asdl import (
      redir, env_pair, proc_sig__Closed,
  )
  from core.alloc import Arena
  from core import optview
  from core.vm import _Executor, _AssignBuiltin
  from oil_lang import expr_eval
  from osh import word_eval
  from osh import builtin_process

# flags for main_loop.Batch, ExecuteAndCatch.  TODO: Should probably in
# ExecuteAndCatch, along with SetValue() flags.
IsMainProgram = 1 << 0  # the main shell program, not eval/source/subshell
RaiseControlFlow = 1 << 1  # eval/source builtins
Optimize = 1 << 2


# Python type name -> Oil type name
OIL_TYPE_NAMES = {
    'bool': 'Bool',
    'int': 'Int',
    'float': 'Float',
    'str': 'Str',
    'tuple': 'Tuple',
    'list': 'List',
    'dict': 'Dict',
}


class Deps(object):
  def __init__(self):
    # type: () -> None
    self.mutable_opts = None  # type: state.MutableOpts
    self.dumper = None        # type: dev.CrashDumper
    self.debug_f = None       # type: util._DebugFile

    # signal/hook name -> handler
    self.traps = None         # type: Dict[str, builtin_process._TrapHandler]
    # appended to by signal handlers
    self.trap_nodes = None    # type: List[command_t]


if mylib.PYTHON:
  def _PyObjectToVal(py_val):
    # type: (Any) -> Any
    """
    Maintain the 'value' invariant in osh/runtime.asdl.

    TODO: Move this to Mem and combine with LookupVar in oil_lang/expr_eval.py.
    They are opposites.
    """
    if isinstance(py_val, str):  # var s = "hello $name"
      val = value.Str(py_val)  # type: Any
    elif isinstance(py_val, objects.StrArray):  # var a = @(a b)
      # It's safe to convert StrArray to MaybeStrArray.
      val = value.MaybeStrArray(py_val)
    elif isinstance(py_val, dict):  # var d = {name: "bob"}
      # TODO: Is this necessary?  Shell assoc arrays aren't nested and don't have
      # arbitrary values.
      val = value.AssocArray(py_val)
    else:
      val = value.Obj(py_val)
    return val


def _PackFlags(keyword_id, flags=0):
  # type: (Id_t, int) -> int

  # Set/Clear are lower 8 bits, and keyword is the rest
  return (keyword_id << 8) | flags


def _HasManyStatuses(node):
  # type: (command_t) -> bool
  """
  Code patterns that are bad for POSIX errexit.  For Oil's strict_errexit.
  """
  if node.tag_() == command_e.Sentence:
    node1 = cast(command__Sentence, node)
    return _HasManyStatuses(node1.child)

  # allow list under strict_errexit
  UP_node = node
  with tagswitch(node) as case:
    # command subs in words are detected by allow_command_sub in ctx_ErrExit
    if case(command_e.Simple, command_e.DBracket, command_e.DParen):
      return False

    elif case(command_e.Pipeline):
      # TODO: Allow it because set -o pipefail takes all exit codes into
      # account.
      return False

  return True


class CommandEvaluator(object):
  """Executes the program by tree-walking.

  It also does some double-dispatch by passing itself into Eval() for
  Compound/WordPart.
  """
  def __init__(self,
               mem,              # type: state.Mem
               exec_opts,        # type: optview.Exec
               errfmt,           # type: ui.ErrorFormatter
               procs,            # type: Dict[str, Proc]
               assign_builtins,  # type: Dict[builtin_t, _AssignBuiltin]
               arena,            # type: Arena
               cmd_deps,         # type: Deps
  ):
    # type: (...) -> None
    """
    Args:
      mem: Mem instance for storing variables
      procs: dict of SHELL functions or 'procs'
      builtins: dict of builtin callables
                TODO: This should only be for assignment builtins?
      cmd_deps: A bundle of stateless code
    """
    self.shell_ex = None  # type: _Executor
    self.arith_ev = None  # type: sh_expr_eval.ArithEvaluator
    self.bool_ev = None  # type: sh_expr_eval.BoolEvaluator
    self.expr_ev = None  # type: expr_eval.OilEvaluator
    self.word_ev = None  # type: word_eval.AbstractWordEvaluator
    self.tracer = None  # type: dev.Tracer

    self.mem = mem
    # This is for shopt and set -o.  They are initialized by flags.
    self.exec_opts = exec_opts
    self.errfmt = errfmt
    self.procs = procs
    self.assign_builtins = assign_builtins
    self.arena = arena

    self.mutable_opts = cmd_deps.mutable_opts
    self.dumper = cmd_deps.dumper
    self.debug_f = cmd_deps.debug_f  # Used by ShellFuncAction too

    self.traps = cmd_deps.traps
    self.trap_nodes = cmd_deps.trap_nodes

    self.loop_level = 0  # for detecting bad top-level break/continue
    self.check_command_sub_status = False  # a hack.  Modified by ShellExecutor

  def CheckCircularDeps(self):
    # type: () -> None
    assert self.arith_ev is not None
    assert self.bool_ev is not None
    # Disabled for push OSH
    #assert self.expr_ev is not None
    assert self.word_ev is not None

  def _RunAssignBuiltin(self, cmd_val):
    # type: (cmd_value__Assign) -> int
    """Run an assignment builtin.  Except blocks copied from RunBuiltin."""
    self.errfmt.PushLocation(cmd_val.arg_spids[0])  # default

    builtin_func = self.assign_builtins.get(cmd_val.builtin_id)
    if builtin_func is None:
      # This only happens with alternative Oil interpreters.
      e_die("Assignment builtin %r not configured",
            cmd_val.argv[0], span_id=cmd_val.arg_spids[0])

    try:
      status = builtin_func.Run(cmd_val)
    except error.Usage as e:  # Copied from RunBuiltin
      arg0 = cmd_val.argv[0]
      if e.span_id == runtime.NO_SPID:  # fill in default location.
        e.span_id = self.errfmt.CurrentLocation()
      self.errfmt.PrefixPrint(e.msg, prefix='%r ' % arg0, span_id=e.span_id)
      status = 2  # consistent error code for usage error
    except KeyboardInterrupt:
      if self.exec_opts.interactive():
        print('')  # newline after ^C
        status = 130  # 128 + 2 for SIGINT
      else:
        raise
    finally:
      try:
        sys.stdout.flush()
      except IOError as e:
        pass

      self.errfmt.PopLocation()

    return status

  # TODO: Also change to BareAssign (set global or mutate local) and
  # KeywordAssign.  The latter may have flags too.
  def _SpanIdForShAssignment(self, node):
    # type: (command__ShAssignment) -> int
    # TODO: Share with tracing (SetCurrentSpanId) and _CheckStatus
    return node.spids[0]

  def _CheckStatus(self, status, node, blame_spid):
    # type: (int, command_t, int) -> None
    """Raises error.ErrExit, maybe with location info attached."""
    if self.exec_opts.errexit() and status != 0:
      # NOTE: Sometimes location info is duplicated.
      # - 'type -z' has a UsageError with location, then errexit
      # - '> /nonexistent' has an I/O error, then errexit
      # - Pipelines and subshells are compound.  Commands within them fail.
      #   - however ( exit 33 ) only prints one message.
      #
      # But we will want something like 'false' to have location info.

      UP_node = node
      with tagswitch(node) as case:
        if case(command_e.Simple):
          node = cast(command__Simple, UP_node)
          reason = 'command in '
          span_id = location.SpanForCommand(node)
        elif case(command_e.ShAssignment):
          node = cast(command__ShAssignment, UP_node)
          reason = 'assignment in '
          span_id = self._SpanIdForShAssignment(node)

        # Note: a subshell often doesn't fail on its own.
        elif case(command_e.Subshell):
          node = cast(command__Subshell, UP_node)
          reason = 'subshell invoked from '
          span_id = node.spids[0]
        elif case(command_e.Pipeline):
          node = cast(command__Pipeline, UP_node)
          # The whole pipeline can fail separately
          # TODO: We should show which element of the pipeline failed!
          reason = 'pipeline invoked from '
          span_id = node.spids[0]  # spid of !, or first |
        else:
          # NOTE: The fallback of CurrentSpanId() fills this in.
          reason = ''
          span_id = runtime.NO_SPID

      # override if explicitly passed, e.g. for process sub
      if blame_spid != runtime.NO_SPID:
        span_id = blame_spid

      raise error.ErrExit(
          'Exiting with status %d (%sPID %d)' % (status, reason, posix.getpid()),
          span_id=span_id, status=status)

  def _EvalRedirect(self, r):
    # type: (redir) -> redirect

    result = redirect(r.op.id, r.op.span_id, r.loc, None)

    arg = r.arg
    UP_arg = arg
    with tagswitch(arg) as case:
      if case(redir_param_e.Word):
        arg_word = cast(compound_word, UP_arg)

        # note: needed for redirect like 'echo foo > x$LINENO'
        self.mem.SetCurrentSpanId(r.op.span_id)

        redir_type = consts.RedirArgType(r.op.id)  # could be static in the LST?

        if redir_type == redir_arg_type_e.Path:
          # NOTES
          # - no globbing.  You can write to a file called '*.py'.
          # - set -o strict-array prevents joining by spaces
          val = self.word_ev.EvalWordToString(arg_word)
          filename = val.s
          if len(filename) == 0:
            # Whether this is fatal depends on errexit.
            raise error.RedirectEval(
                "Redirect filename can't be empty", word=arg_word)

          result.arg = redirect_arg.Path(filename)
          return result

        elif redir_type == redir_arg_type_e.Desc:  # e.g. 1>&2, 1>&-, 1>&2-
          val = self.word_ev.EvalWordToString(arg_word)
          t = val.s
          if len(t) == 0:
            raise error.RedirectEval(
                "Redirect descriptor can't be empty", word=arg_word)
            return None

          try:
            if t == '-':
              result.arg = redirect_arg.CloseFd()
            elif t[-1] == '-':
              target_fd = int(t[:-1])
              result.arg = redirect_arg.MoveFd(target_fd)
            else:
              result.arg = redirect_arg.CopyFd(int(t))
          except ValueError:
            raise error.RedirectEval(
                'Invalid descriptor %r.  Expected D, -, or D- where D is an '
                'integer' % t, word=arg_word)
            return None

          return result

        elif redir_type == redir_arg_type_e.Here:  # here word
          val = self.word_ev.EvalWordToString(arg_word)
          assert val.tag_() == value_e.Str, val
          # NOTE: bash and mksh both add \n
          result.arg = redirect_arg.HereDoc(val.s + '\n')
          return result

        else:
          raise AssertionError('Unknown redirect op')

      elif case(redir_param_e.HereDoc):
        arg = cast(redir_param__HereDoc, UP_arg)
        w = compound_word(arg.stdin_parts)  # HACK: Wrap it in a word to eval
        val = self.word_ev.EvalWordToString(w)
        assert val.tag_() == value_e.Str, val
        result.arg = redirect_arg.HereDoc(val.s)
        return result

      else:
        raise AssertionError('Unknown redirect type')

  def _EvalRedirects(self, node):
    # type: (command_t) -> List[redirect]
    """Evaluate redirect nodes to concrete objects.

    We have to do this every time, because you could have something like:

    for i in a b c; do
      echo foo >$i
    done

    Does it makes sense to just have RedirectNode.Eval?  Nah I think the
    Redirect() abstraction in process.py is useful.  It has a lot of methods.

    Raises:
      error.RedirectEval
    """
    # This is kind of lame because we have two switches over command_e: one for
    # redirects, and to evaluate the node.  But it's what you would do in C++ I
    # suppose.  We could also inline them.  Or maybe use RAII.
    UP_node = node
    with tagswitch(node) as case:
      if case(command_e.Simple):
        node = cast(command__Simple, UP_node)
        redirects = node.redirects
      elif case(command_e.ExpandedAlias):
        node = cast(command__ExpandedAlias, UP_node)
        redirects = node.redirects
      elif case(command_e.ShAssignment):
        node = cast(command__ShAssignment, UP_node)
        redirects = node.redirects
      elif case(command_e.BraceGroup):
        node = cast(BraceGroup, UP_node)
        redirects = node.redirects
      elif case(command_e.Subshell):
        node = cast(command__Subshell, UP_node)
        redirects = node.redirects
      elif case(command_e.DParen):
        node = cast(command__DParen, UP_node)
        redirects = node.redirects
      elif case(command_e.DBracket):
        node = cast(command__DBracket, UP_node)
        redirects = node.redirects
      elif case(command_e.ForEach):
        node = cast(command__ForEach, UP_node)
        redirects = node.redirects
      elif case(command_e.ForExpr):
        node = cast(command__ForExpr, UP_node)
        redirects = node.redirects
      elif case(command_e.WhileUntil):
        node = cast(command__WhileUntil, UP_node)
        redirects = node.redirects
      elif case(command_e.If):
        node = cast(command__If, UP_node)
        redirects = node.redirects
      elif case(command_e.Case):
        node = cast(command__Case, UP_node)
        redirects = node.redirects
      else:
        # command_e.NoOp, command_e.ControlFlow, command_e.Pipeline,
        # command_e.AndOr, command_e.CommandList, command_e.DoGroup,
        # command_e.Sentence, # command_e.TimeBlock, command_e.ShFunction,
        # Oil:
        # command_e.VarDecl, command_e.PlaceMutation, command_e.OilForIn,
        # command_e.Proc, command_e.Func, command_e.Expr,
        # command_e.BareDecl
        redirects = []

    result = []  # type: List[redirect]
    for redir in redirects:
      result.append(self._EvalRedirect(redir))
    return result

  def _RunSimpleCommand(self, cmd_val, do_fork):
    # type: (cmd_value_t, bool) -> int
    """Private interface to run a simple command (including assignment)."""
    UP_cmd_val = cmd_val
    with tagswitch(UP_cmd_val) as case:
      if case(cmd_value_e.Argv):
        cmd_val = cast(cmd_value__Argv, UP_cmd_val)
        self.tracer.OnSimpleCommand(cmd_val.argv)
        return self.shell_ex.RunSimpleCommand(cmd_val, do_fork)

      elif case(cmd_value_e.Assign):
        cmd_val = cast(cmd_value__Assign, UP_cmd_val)
        self.tracer.OnAssignBuiltin(cmd_val)
        return self._RunAssignBuiltin(cmd_val)

      else:
        raise AssertionError()

  def _EvalTempEnv(self, more_env, flags):
    # type: (List[env_pair], int) -> None
    """For FOO=1 cmd."""
    for e_pair in more_env:
      val = self.word_ev.EvalWordToString(e_pair.val)
      # Set each var so the next one can reference it.  Example:
      # FOO=1 BAR=$FOO ls /
      self.mem.SetValue(lvalue.Named(e_pair.name), val, scope_e.LocalOnly,
                        flags=flags)

  def _StrictErrExit(self, node):
    # type: (command_t) -> None
    if not (self.exec_opts.errexit() and self.exec_opts.strict_errexit()):
      return

    if _HasManyStatuses(node):
      node_str = ui.CommandType(node)
      e_die("strict_errexit only allows simple commands (got %s). "
            "Hint: use 'try'.",
            node_str, span_id=location.SpanForCommand(node))

  def _StrictErrExitList(self, node_list):
    # type: (List[command_t]) -> None
    """
    Not allowed, too confusing:

    if grep foo eggs.txt; grep bar eggs.txt; then
      echo hi
    fi
    """
    if not (self.exec_opts.errexit() and self.exec_opts.strict_errexit()):
      return

    if len(node_list) > 1:
      e_die('strict_errexit only allows a single command.  Hint: use "run".',
            span_id=location.SpanForCommand(node_list[0]))

    assert len(node_list) > 0
    node = node_list[0]
    if _HasManyStatuses(node):  # TODO: consolidate error message with above
      node_str = ui.CommandType(node)
      e_die("strict_errexit only allows simple commands (got %s). "
            "Hint: use 'try'.",
            node_str, span_id=location.SpanForCommand(node))

  def _EvalCondition(self, cond, spid):
    # type: (condition_t, int) -> bool
    b = False
    UP_cond = cond
    with tagswitch(cond) as case:
      if case(condition_e.Shell):
        cond = cast(condition__Shell, UP_cond)
        self._StrictErrExitList(cond.commands)
        with state.ctx_ErrExit(self.mutable_opts, False, spid):
          cond_status = self._ExecuteList(cond.commands)

        b = cond_status == 0

      elif case(condition_e.Oil):
        if mylib.PYTHON:
          cond = cast(condition__Oil, UP_cond)
          obj = self.expr_ev.EvalExpr(cond.e)
          b = bool(obj)

    return b

  def _Dispatch(self, node, pipeline_st):
    # type: (command_t, CompoundStatus) -> Tuple[int, bool]
    """Switch on the command_t variants and execute them."""

    # If we call RunCommandSub in a recursive call to the executor, this will
    # be set true (if strict_errexit is false).  But it only lasts for one
    # command.
    self.check_command_sub_status = False

    check_errexit = False  # for errexit

    UP_node = node
    with tagswitch(node) as case:
      if case(command_e.Simple):
        node = cast(command__Simple, UP_node)
        check_errexit = True

        # Find span_id for a basic implementation of $LINENO, e.g.
        # PS4='+$SOURCE_NAME:$LINENO:'
        # Note that for '> $LINENO' the span_id is set in _EvalRedirect.
        # TODO: Can we avoid setting this so many times?  See issue #567.
        if len(node.words):
          span_id = word_.LeftMostSpanForWord(node.words[0])
          # Special case for __cat < file: leave it at the redirect.
          if span_id != runtime.NO_SPID:
            self.mem.SetCurrentSpanId(span_id)

        # PROBLEM: We want to log argv in 'xtrace' mode, but we may have already
        # redirected here, which screws up logging.  For example, 'echo hi
        # >/dev/null 2>&1'.  We want to evaluate argv and log it BEFORE applying
        # redirects.

        # Another problem:
        # - tracing can be called concurrently from multiple processes, leading
        # to overlap.  Maybe have a mode that creates a file per process.
        # xtrace-proc
        # - line numbers for every command would be very nice.  But then you have
        # to print the filename too.

        words = braces.BraceExpandWords(node.words)

        # Note: Individual WORDS can fail
        # - $() and <() can have failures.  This can happen in DBracket,
        #   DParen, etc. too
        # - we could catch the 'failglob' exception here and return 1, e.g. *.bad
        # - Tracing: this can start processes for proc sub and here docs!
        cmd_val = self.word_ev.EvalWordSequence2(words, allow_assign=True)

        UP_cmd_val = cmd_val
        if UP_cmd_val.tag_() == cmd_value_e.Argv:
          cmd_val = cast(cmd_value__Argv, UP_cmd_val)

          typed_args = None  # type: ArgList
          if node.typed_args:
            # Copy positional args because we may append an arg
            typed_args = ArgList(list(node.typed_args.positional), node.typed_args.named)
            typed_args.spids = node.typed_args.spids

            # the block is the last argument
            if node.block:
              block_expr = expr__BlockArg(node.block)
              typed_args.positional.append(block_expr)
              # ArgList already has a spid in this case
          else:
            if node.block:
              # create ArgList for the block
              typed_args = ArgList()
              block_expr = expr__BlockArg(node.block)
              typed_args.positional.append(block_expr)
              typed_args.spids.append(node.block.spids[0])
          cmd_val.typed_args = typed_args
       
        else:
          if node.block:
            e_die("ShAssignment builtins don't accept blocks",
                  span_id=node.block.spids[0])
          cmd_val = cast(cmd_value__Assign, UP_cmd_val)

        # NOTE: RunSimpleCommand never returns when do_fork=False!
        if len(node.more_env):  # I think this guard is necessary?
          is_other_special = False  # TODO: There are other special builtins too!
          if cmd_val.tag_() == cmd_value_e.Assign or is_other_special:
            # Special builtins have their temp env persisted.
            self._EvalTempEnv(node.more_env, 0)
            status = self._RunSimpleCommand(cmd_val, node.do_fork)
          else:
            with state.ctx_Temp(self.mem):
              self._EvalTempEnv(node.more_env, state.SetExport)
              status = self._RunSimpleCommand(cmd_val, node.do_fork)
        else:
          status = self._RunSimpleCommand(cmd_val, node.do_fork)

      elif case(command_e.ExpandedAlias):
        node = cast(command__ExpandedAlias, UP_node)
        # Expanded aliases need redirects and env bindings from the calling
        # context, as well as redirects in the expansion!

        # TODO: SetCurrentSpanId to OUTSIDE?  Don't bother with stuff inside
        # expansion, since aliases are discouarged.

        if len(node.more_env):
          with state.ctx_Temp(self.mem):
            self._EvalTempEnv(node.more_env, state.SetExport)
            status = self._Execute(node.child)
        else:
          status = self._Execute(node.child)

      elif case(command_e.Sentence):
        node = cast(command__Sentence, UP_node)
        # Don't check_errexit since this isn't a real node!
        if node.terminator.id == Id.Op_Semi:
          status = self._Execute(node.child)
        else:
          status = self.shell_ex.RunBackgroundJob(node.child)

      elif case(command_e.Pipeline):
        node = cast(command__Pipeline, UP_node)
        check_errexit = True
        if len(node.stderr_indices):
          e_die("|& isn't supported", span_id=node.spids[0])

        # TODO: how to get errexit_spid into _Execute?
        # It can be the span_id of !, or of the pipeline component that failed,
        # recorded in c_status.
        if node.negated:
          self._StrictErrExit(node)
          pipeline_st.negated = True
          # spid of !
          with state.ctx_ErrExit(self.mutable_opts, False, node.spids[0]):
            self.shell_ex.RunPipeline(node, pipeline_st)

          # errexit is disabled for !.
          check_errexit = False
        else:
          self.shell_ex.RunPipeline(node, pipeline_st)

        status = -1  # INVALID value because the caller will compute it

      elif case(command_e.Subshell):
        node = cast(command__Subshell, UP_node)
        check_errexit = True
        status = self.shell_ex.RunSubshell(node.child)

      elif case(command_e.DBracket):
        node = cast(command__DBracket, UP_node)
        left_spid = node.spids[0]
        self.mem.SetCurrentSpanId(left_spid)

        self.tracer.PrintSourceCode(left_spid, node.spids[1], self.arena)

        check_errexit = True
        result = self.bool_ev.EvalB(node.expr)
        status = 0 if result else 1

      elif case(command_e.DParen):
        node = cast(command__DParen, UP_node)
        left_spid = node.spids[0]
        self.mem.SetCurrentSpanId(left_spid)

        self.tracer.PrintSourceCode(left_spid, node.spids[1], self.arena)

        check_errexit = True
        i = self.arith_ev.EvalToInt(node.child)
        status = 1 if i == 0 else 0

      # TODO: Change x = 1 + 2*3 into its own Decl node.
      elif case(command_e.VarDecl):
        node = cast(command__VarDecl, UP_node)

        if mylib.PYTHON:
          # x = 'foo' is a shortcut for const x = 'foo'
          if node.keyword is None or node.keyword.id == Id.KW_Const:
            self.mem.SetCurrentSpanId(node.lhs[0].name.span_id)  # point to var name

            # Note: there's only one LHS
            vd_lval = lvalue.Named(node.lhs[0].name.val)  # type: lvalue_t
            try:
              py_val = self.expr_ev.EvalExpr(node.rhs)
            except TypeError as e:
              # TODO: We need better location info, as we blame the var name now
              # This also relies on errors from CPython, so the errors aren't
              # great.  For example 'mystr->bad' says "string indices must be
              # integers" because it's really mystr['bad']
              e_die('Type error in expression: %s', str(e))

            val = _PyObjectToVal(py_val)  # type: value_t

            self.mem.SetValue(vd_lval, val, scope_e.LocalOnly, 
                              flags=_PackFlags(Id.KW_Const, state.SetReadOnly))
            status = 0

          else:
            self.mem.SetCurrentSpanId(node.keyword.span_id)  # point to var

            py_val = self.expr_ev.EvalExpr(node.rhs)
            vd_lvals = []  # type: List[lvalue_t]
            vals = []  # type: List[value_t]
            if len(node.lhs) == 1:  # TODO: optimize this common case (but measure)
              vd_lval = lvalue.Named(node.lhs[0].name.val)
              val = _PyObjectToVal(py_val)

              vd_lvals.append(vd_lval)
              vals.append(val)
            else:
              it = py_val.__iter__()
              for vd_lhs in node.lhs:
                vd_lval = lvalue.Named(vd_lhs.name.val)
                val = _PyObjectToVal(it.next())

                vd_lvals.append(vd_lval)
                vals.append(val)

            for vd_lval, val in zip(vd_lvals, vals):
              self.mem.SetValue(vd_lval, val, scope_e.LocalOnly,
                                flags=_PackFlags(node.keyword.id))

          status = 0

      elif case(command_e.PlaceMutation):

        if mylib.PYTHON:  # DISABLED because it relies on CPytho now
          node = cast(command__PlaceMutation, UP_node)
          self.mem.SetCurrentSpanId(node.keyword.span_id)  # point to setvar/set

          with switch(node.keyword.id) as case2:
            if case2(Id.KW_SetVar):
              which_scopes = scope_e.LocalOnly
            elif case2(Id.KW_SetGlobal):
              which_scopes = scope_e.GlobalOnly
            elif case2(Id.KW_SetRef):
              # The out param is LOCAL, but the nameref lookup is dynamic
              which_scopes = scope_e.LocalOnly
            else:
              raise AssertionError(node.keyword.id)

          if node.op.id == Id.Arith_Equal:
            py_val = self.expr_ev.EvalExpr(node.rhs)

            lvals_ = []  # type: List[lvalue_t]
            py_vals = []
            if len(node.lhs) == 1:  # TODO: Optimize this common case (but measure)
              # See ShAssignment
              lval_ = self.expr_ev.EvalPlaceExpr(node.lhs[0]) # type: lvalue_t

              lvals_.append(lval_)
              py_vals.append(py_val)
            else:
              it = py_val.__iter__()
              for pm_lhs in node.lhs:
                lval_ = self.expr_ev.EvalPlaceExpr(pm_lhs)
                py_val = it.next()

                lvals_.append(lval_)
                py_vals.append(py_val)

            # TODO: Resolve the asymmetry betwen Named vs ObjIndex,ObjAttr.
            for UP_lval_, py_val in zip(lvals_, py_vals):
              tag = UP_lval_.tag_()
              if tag == lvalue_e.ObjIndex:
                lval_ = cast(lvalue__ObjIndex, UP_lval_)
                lval_.obj[lval_.index] = py_val
                if node.keyword.id == Id.KW_SetRef:
                  e_die('setref obj[index] not implemented')
              elif tag == lvalue_e.ObjAttr:
                lval_ = cast(lvalue__ObjAttr, UP_lval_)
                setattr(lval_.obj, lval_.attr, py_val)
                if node.keyword.id == Id.KW_SetRef:
                  e_die('setref obj.attr not implemented')
              else:
                val = _PyObjectToVal(py_val)
                # top level variable
                self.mem.SetValue(UP_lval_, val, which_scopes,
                                  flags=_PackFlags(node.keyword.id))

          # TODO: Other augmented assignments
          elif node.op.id == Id.Arith_PlusEqual:
            # NOTE: x, y += 1 in Python is a SYNTAX error, but it's checked in the
            # transformer and not the grammar.  We should do that too.

            place_expr = cast(place_expr__Var, node.lhs[0])
            pe_lval = lvalue.Named(place_expr.name.val)
            py_val = self.expr_ev.EvalExpr(node.rhs)

            new_py_val = self.expr_ev.EvalPlusEquals(pe_lval, py_val)
            # This should only be an int or float, so we don't need the logic above
            val = value.Obj(new_py_val)

            self.mem.SetValue(pe_lval, val, which_scopes,
                            flags=_PackFlags(node.keyword.id))

          else:
            raise NotImplementedError(Id_str(node.op.id))

        status = 0  # TODO: what should status be?

      elif case(command_e.ShAssignment):  # Only unqualified assignment
        node = cast(command__ShAssignment, UP_node)

        # x=y is 'neutered' inside 'proc'
        which_scopes = self.mem.ScopesForWriting()

        for pair in node.pairs:
          spid = pair.spids[0]  # Source location for tracing
          # Use the spid of each pair.
          self.mem.SetCurrentSpanId(spid)

          if pair.op == assign_op_e.PlusEqual:
            assert pair.rhs, pair.rhs  # I don't think a+= is valid?
            val = self.word_ev.EvalRhsWord(pair.rhs)

            lval = self.arith_ev.EvalShellLhs(pair.lhs, spid, which_scopes)
            old_val = sh_expr_eval.OldValue(lval, self.mem, self.exec_opts)

            UP_old_val = old_val
            UP_val = val

            old_tag = old_val.tag_()
            tag = val.tag_()

            if old_tag == value_e.Undef and tag == value_e.Str:
              pass  # val is RHS
            elif old_tag == value_e.Undef and tag == value_e.MaybeStrArray:
              pass  # val is RHS

            elif old_tag == value_e.Str and tag == value_e.Str:
              old_val = cast(value__Str, UP_old_val)
              str_to_append = cast(value__Str, UP_val)
              val = value.Str(old_val.s + str_to_append.s)

            elif old_tag == value_e.Str and tag == value_e.MaybeStrArray:
              e_die("Can't append array to string")

            elif old_tag == value_e.MaybeStrArray and tag == value_e.Str:
              e_die("Can't append string to array")

            elif (old_tag == value_e.MaybeStrArray and
                  tag == value_e.MaybeStrArray):
              old_val = cast(value__MaybeStrArray, UP_old_val)
              to_append = cast(value__MaybeStrArray, UP_val)

              # TODO: MUTATE the existing value for efficiency?
              strs = []  # type: List[str]
              strs.extend(old_val.strs)
              strs.extend(to_append.strs)
              val = value.MaybeStrArray(strs)

          else:  # plain assignment
            lval = self.arith_ev.EvalShellLhs(pair.lhs, spid, which_scopes)

            # RHS can be a string or array.
            if pair.rhs:
              val = self.word_ev.EvalRhsWord(pair.rhs)
              assert isinstance(val, value_t), val

            else:  # e.g. 'readonly x' or 'local x'
              val = None

          # NOTE: In bash and mksh, declare -a myarray makes an empty cell with
          # Undef value, but the 'array' attribute.

          #log('setting %s to %s with flags %s', lval, val, flags)
          flags = 0
          self.mem.SetValue(lval, val, which_scopes, flags=flags)
          self.tracer.OnShAssignment(lval, pair.op, val, flags, which_scopes)

        # PATCH to be compatible with existing shells: If the assignment had a
        # command sub like:
        #
        # s=$(echo one; false)
        #
        # then its status will be in mem.last_status, and we can check it here.
        # If there was NOT a command sub in the assignment, then we don't want to
        # check it.

        # Only do this if there was a command sub?  How?  Look at node?
        # Set a flag in mem?   self.mem.last_status or
        if self.check_command_sub_status:
          last_status = self.mem.LastStatus()
          self._CheckStatus(last_status, node, runtime.NO_SPID)
          status = last_status  # A global assignment shouldn't clear $?.
        else:
          status = 0

      elif case(command_e.Expr):
        node = cast(command__Expr, UP_node)

        if mylib.PYTHON:
          self.mem.SetCurrentSpanId(node.keyword.span_id)
          obj = self.expr_ev.EvalExpr(node.e)

          if node.keyword.id == Id.Lit_Equals:
            # NOTE: It would be nice to unify this with 'repr', but there isn't a
            # good way to do it with the value/PyObject split.
            class_name = obj.__class__.__name__
            oil_name = OIL_TYPE_NAMES.get(class_name, class_name)
            print('(%s)   %s' % (oil_name, repr(obj)))

        # TODO: What about exceptions?  They just throw?
        status = 0

      elif case(command_e.ControlFlow):
        node = cast(command__ControlFlow, UP_node)
        tok = node.token

        if node.arg_word:  # Evaluate the argument
          str_val = self.word_ev.EvalWordToString(node.arg_word)

          # This is an OVERLOADING of strict_control_flow, which also has to do
          # with break/continue at top level.  # We need 'return $empty' to be valid
          # for libtool.  It also has the side effect of making 'return ""'
          # valid, which shells other than zsh fail on.  That seems OK.
          if len(str_val.s) == 0 and not self.exec_opts.strict_control_flow():
            arg = 0
          else:
            try:
              # They all take integers.  NOTE: dash is the only shell that
              # disallows -1!  Others wrap to 255.
              arg = int(str_val.s)
            except ValueError:
              e_die('%r expected a number, got %r',
                    node.token.val, str_val.s, word=node.arg_word)
        else:
          if tok.id in (Id.ControlFlow_Exit, Id.ControlFlow_Return):
            arg = self.mem.LastStatus()
          else:
            arg = 0  # break 0 levels, nothing for continue

        self.tracer.OnControlFlow(tok.val, arg)

        # NOTE: A top-level 'return' is OK, unlike in bash.  If you can return
        # from a sourced script, it makes sense to return from a main script.
        ok = True
        if (tok.id in (Id.ControlFlow_Break, Id.ControlFlow_Continue) and
            self.loop_level == 0):
          ok = False

        if ok:
          if tok.id == Id.ControlFlow_Exit:
            raise util.UserExit(arg)  # handled differently than other control flow
          else:
            raise _ControlFlow(tok, arg)
        else:
          msg = 'Invalid control flow at top level'
          if self.exec_opts.strict_control_flow():
            e_die(msg, token=tok)
          else:
            # Only print warnings, never fatal.
            # Bash oddly only exits 1 for 'return', but no other shell does.
            self.errfmt.PrefixPrint(msg, prefix='warning: ', span_id=tok.span_id)
            status = 0

      # Note CommandList and DoGroup have no redirects, but BraceGroup does.
      # DoGroup has 'do' and 'done' spids for translation.
      elif case(command_e.CommandList):
        node = cast(command__CommandList, UP_node)
        status = self._ExecuteList(node.children)
        check_errexit = False

      elif case(command_e.DoGroup):
        node = cast(command__DoGroup, UP_node)
        status = self._ExecuteList(node.children)
        check_errexit = False  # not real statements

      elif case(command_e.BraceGroup):
        node = cast(BraceGroup, UP_node)
        status = self._ExecuteList(node.children)
        check_errexit = False

      elif case(command_e.AndOr):
        node = cast(command__AndOr, UP_node)
        # NOTE: && and || have EQUAL precedence in command mode.  See case #13
        # in dbracket.test.sh.

        left = node.children[0]

        # Suppress failure for every child except the last one.
        self._StrictErrExit(left)
        with state.ctx_ErrExit(self.mutable_opts, False, node.spids[0]):
          status = self._Execute(left)

        i = 1
        n = len(node.children)
        while i < n:
          #log('i %d status %d', i, status)
          child = node.children[i]
          op_id = node.ops[i-1]

          #log('child %s op_id %s', child, op_id)

          if op_id == Id.Op_DPipe and status == 0:
            i += 1
            continue  # short circuit

          elif op_id == Id.Op_DAmp and status != 0:
            i += 1
            continue  # short circuit

          if i == n - 1:  # errexit handled differently for last child
            status = self._Execute(child)
            check_errexit = True
          else:
            # blame the right && or ||
            self._StrictErrExit(child)
            with state.ctx_ErrExit(self.mutable_opts, False, node.spids[i]):
              status = self._Execute(child)

          i += 1

      elif case(command_e.WhileUntil):
        node = cast(command__WhileUntil, UP_node)
        status = 0

        self.loop_level += 1
        try:
          while True:
            try:
              # blame while/until spid
              b = self._EvalCondition(node.cond, node.spids[0])
              if node.keyword.id == Id.KW_Until:
                b = not b
              if not b:
                break
              status = self._Execute(node.body)  # last one wins

            except _ControlFlow as e:
              # Important: 'break' can occur in the CONDITION or body
              if e.IsBreak():
                status = 0
                break
              elif e.IsContinue():
                status = 0
                continue
              else:  # return needs to pop up more
                raise
        finally:
          self.loop_level -= 1

      elif case(command_e.ForEach):
        node = cast(command__ForEach, UP_node)
        self.mem.SetCurrentSpanId(node.spids[0])  # for x in $LINENO

        iter_name = node.iter_name
        if node.do_arg_iter:
          iter_list = self.mem.GetArgv()
        else:
          words = braces.BraceExpandWords(node.iter_words)
          iter_list = self.word_ev.EvalWordSequence(words)

        status = 0  # in case we don't loop
        self.loop_level += 1
        try:
          for x in iter_list:
            #log('> ForEach setting %r', x)
            self.mem.SetValue(lvalue.Named(iter_name), value.Str(x),
                              scope_e.LocalOnly)
            #log('<')

            try:
              status = self._Execute(node.body)  # last one wins
            except _ControlFlow as e:
              if e.IsBreak():
                status = 0
                break
              elif e.IsContinue():
                status = 0
              else:  # return needs to pop up more
                raise
        finally:
          self.loop_level -= 1

      elif case(command_e.ForExpr):
        node = cast(command__ForExpr, UP_node)
        status = 0

        init = node.init
        for_cond = node.cond
        body = node.body
        update = node.update

        if init:
          self.arith_ev.Eval(init)

        self.loop_level += 1
        try:
          while True:
            if for_cond:
              # We only accept integers as conditions
              cond_int = self.arith_ev.EvalToInt(for_cond)
              if cond_int == 0:  # false
                break

            try:
              status = self._Execute(body)
            except _ControlFlow as e:
              if e.IsBreak():
                status = 0
                break
              elif e.IsContinue():
                status = 0
              else:  # return needs to pop up more
                raise

            if update:
              self.arith_ev.Eval(update)

        finally:
          self.loop_level -= 1

      elif case(command_e.OilForIn):
        node = cast(command__OilForIn, UP_node)
        # NOTE: This is a metacircular implementation using the iterable
        # protocol.
        status = 0

        if mylib.PYTHON:
          obj = self.expr_ev.EvalExpr(node.iterable)
          if isinstance(obj, str):
            e_die("Strings aren't iterable")
          else:
            it = obj.__iter__()

          body = node.body
          iter_name = node.lhs[0].name.val  # TODO: proper lvalue
          while True:
            try:
              loop_val = it.next()
            except StopIteration:
              break
            self.mem.SetValue(lvalue.Named(iter_name),
                              _PyObjectToVal(loop_val), scope_e.LocalOnly)

            # Copied from above
            try:
              status = self._Execute(body)
            except _ControlFlow as e:
              if e.IsBreak():
                status = 0
                break
              elif e.IsContinue():
                status = 0
              else:  # return needs to pop up more
                raise

      elif case(command_e.ShFunction):
        node = cast(command__ShFunction, UP_node)
        # name_spid is node.spids[1].  Dynamic scope.
        if node.name in self.procs and not self.exec_opts.redefine_proc():
          e_die("Function %s was already defined (redefine_proc)", node.name,
              span_id=node.spids[1])
        self.procs[node.name] = Proc(
            node.name, node.spids[1], proc_sig.Open(), node.body, [], True)

        status = 0

      elif case(command_e.Proc):
        node = cast(command__Proc, UP_node)

        if node.name.val in self.procs and not self.exec_opts.redefine_proc():
          e_die("Proc %s was already defined (redefine_proc)", node.name.val,
              span_id=node.name.span_id)

        defaults = None  # type: List[value_t]
        if mylib.PYTHON:
          UP_sig = node.sig
          if UP_sig.tag_() == proc_sig_e.Closed:
            sig = cast(proc_sig__Closed, UP_sig)
            defaults = [None] * len(sig.untyped)
            for i, p in enumerate(sig.untyped):
              if p.default_val:
                py_val = self.expr_ev.EvalExpr(p.default_val)
                defaults[i] = _PyObjectToVal(py_val)
     
        self.procs[node.name.val] = Proc(
            node.name.val, node.name.span_id, node.sig, node.body, defaults,
            False)  # no dynamic scope

        status = 0

      elif case(command_e.Func):
        node = cast(command__Func, UP_node)
        # TODO: These should be piped into the tea evaluator ...

        # Note: funcs have the Python pitfall where mutable objects shouldn't be
        # used as default args.

        if mylib.PYTHON:
          pos_defaults = [None] * len(node.pos_params)
          for i, param in enumerate(node.pos_params):
            if param.default_val:
              py_val = self.expr_ev.EvalExpr(param.default_val)
              pos_defaults[i] = _PyObjectToVal(py_val)

          named_defaults = {}
          for i, param in enumerate(node.named_params):
            if param.default_val:
              obj = self.expr_ev.EvalExpr(param.default_val)
              named_defaults[param.name.val] = value.Obj(obj)

          obj = objects.Func(node, pos_defaults, named_defaults, self)
          self.mem.SetValue(
              lvalue.Named(node.name.val), value.Obj(obj), scope_e.GlobalOnly)
        status = 0

      elif case(command_e.If):
        node = cast(command__If, UP_node)
        done = False
        for if_arm in node.arms:
          b = self._EvalCondition(if_arm.cond, if_arm.spids[0])
          if b:
            status = self._ExecuteList(if_arm.action)
            done = True
            break
        # TODO: The compiler should flatten this
        if not done and node.else_action is not None:
          status = self._ExecuteList(node.else_action)

      elif case(command_e.NoOp):
        node = cast(command__NoOp, UP_node)
        status = 0  # make it true

      elif case(command_e.Case):
        node = cast(command__Case, UP_node)
        str_val = self.word_ev.EvalWordToString(node.to_match)
        to_match = str_val.s

        status = 0  # If there are no arms, it should be zero?
        done = False

        for case_arm in node.arms:
          for pat_word in case_arm.pat_list:
            # NOTE: Is it OK that we're evaluating these as we go?
            # TODO: test it out in a loop
            pat_val = self.word_ev.EvalWordToString(pat_word,
                                                    word_eval.QUOTE_FNMATCH)

            #log('Matching word %r against pattern %r', to_match, pat_val.s)
            if libc.fnmatch(pat_val.s, to_match):
              status = self._ExecuteList(case_arm.action)
              done = True  # TODO: Parse ;;& and for fallthrough and such?
              break  # Only execute action ONCE
          if done:
            break

      elif case(command_e.TimeBlock):
        node = cast(command__TimeBlock, UP_node)
        # TODO:
        # - When do we need RUSAGE_CHILDREN?
        # - Respect TIMEFORMAT environment variable.
        # "If this variable is not set, Bash acts as if it had the value"
        # $'\nreal\t%3lR\nuser\t%3lU\nsys\t%3lS'
        # "A trailing newline is added when the format string is displayed."

        s_real, s_user, s_sys = pyos.Time()
        status = self._Execute(node.pipeline)
        e_real, e_user, e_sys = pyos.Time()
        # note: mycpp doesn't support %.3f
        libc.print_time(e_real - s_real, e_user - s_user, e_sys - s_sys)

      else:
        raise NotImplementedError(node.tag_())

    return status, check_errexit

  def RunPendingTraps(self):
    # type: () -> None

    # See osh/builtin_process.py _TrapHandler for the code that appends to this
    # list.
    if len(self.trap_nodes):
      # Make a copy and clear it so we don't cause an infinite loop.
      to_run = list(self.trap_nodes)
      del self.trap_nodes[:]
      with state.ctx_Option(self.mutable_opts, [option_i._running_trap], True):
        for trap_node in to_run:
          # Isolate the exit status.
          with state.ctx_Registers(self.mem): 
            # Trace it.  TODO: Show the trap kind too
            with dev.ctx_Tracer(self.tracer, 'trap', None):
              self._Execute(trap_node)

  def _Execute(self, node):
    # type: (command_t) -> int
    """Apply redirects, call _Dispatch(), and performs the errexit check.

    Also runs trap handlers.
    """
    self.RunPendingTraps()

    # This has to go around redirect handling because the process sub could be
    # in the redirect word:
    #     { echo one; echo two; } > >(tac)

    pipeline_st = CompoundStatus()
    process_sub_st = CompoundStatus()
    errexit_spid = runtime.NO_SPID
    check_errexit = True

    with vm.ctx_ProcessSub(self.shell_ex, process_sub_st):
      try:
        redirects = self._EvalRedirects(node)
      except error.RedirectEval as e:
        ui.PrettyPrintError(e, self.arena)
        redirects = None

      if redirects is None:  # evaluation error
        status = 1

      else:
        # If there are no redirects, push/pop is a no-op.
        if self.shell_ex.PushRedirects(redirects):
          # Asymmetry because of applying redirects can fail.
          with vm.ctx_Redirect(self.shell_ex):
            status, check_errexit = self._Dispatch(node, pipeline_st)

          codes = pipeline_st.codes 
          if len(codes):  # Did we run a pipeline?
            self.mem.SetPipeStatus(codes)

            if self.exec_opts.pipefail():
              # The status is that of the LAST command that is non-zero.
              status = 0
              for i, st in enumerate(codes):
                if st != 0:
                  status = st
                  errexit_spid = pipeline_st.spids[i]
            else:
              status = codes[-1]  # status of last one is pipeline status

            #log('pi st %d', status)
            if pipeline_st.negated:
              status = 1 if status == 0 else 0

        else:
          # Error applying redirects, e.g. bad file descriptor.
          status = 1

    if len(process_sub_st.codes):
      codes = process_sub_st.codes
      self.mem.SetProcessSubStatus(codes)
      if status == 0 and self.exec_opts.process_sub_fail():
        # Choose the LAST one, consistent with pipefail
        for i, st in enumerate(codes):
          if st != 0:
            status = st
            errexit_spid = process_sub_st.spids[i]

    #log('set %d', status)
    self.mem.SetLastStatus(status)

    # NOTE: Bash says that 'set -e' checking is done after each 'pipeline'.
    # However, any bash construct can appear in a pipeline.  So it's easier
    # just to put it at the end, instead of after every node.
    #
    # Possible exceptions:
    # - function def (however this always exits 0 anyway)
    # - assignment - its result should be the result of the RHS?
    #   - e.g. arith sub, command sub?  I don't want arith sub.
    # - ControlFlow: always raises, it has no status.
    if check_errexit:
      self._CheckStatus(status, node, errexit_spid)

    return status

  def _ExecuteList(self, children):
    # type: (List[command_t]) -> int
    status = 0  # for empty list
    for child in children:
      # last status wins
      status = self._Execute(child)
    return status

  def LastStatus(self):
    # type: () -> int
    """For main_loop.py to determine the exit code of the shell itself."""
    return self.mem.LastStatus()

  def _NoForkLast(self, node):
    # type: (command_t) -> None

    if 0:
      log('optimizing')
      node.PrettyPrint(sys.stderr)
      log('')

    UP_node = node
    with tagswitch(node) as case:
      if case(command_e.Simple):
        node = cast(command__Simple, UP_node)
        node.do_fork = False
        if 0:
          log('Simple optimized')

      elif case(command_e.Pipeline):
        node = cast(command__Pipeline, UP_node)
        if not node.negated:
          #log ('pipe')
          self._NoForkLast(node.children[-1])

      elif case(command_e.Sentence):
        node = cast(command__Sentence, UP_node)
        self._NoForkLast(node.child)

      elif case(command_e.CommandList):
        # Subshells start with CommandList, even if there's only one.
        node = cast(command__CommandList, UP_node)
        self._NoForkLast(node.children[-1])

      elif case(command_e.BraceGroup):
        # TODO: What about redirects?
        node = cast(BraceGroup, UP_node)
        self._NoForkLast(node.children[-1])

  def _RemoveSubshells(self, node):
    # type: (command_t) -> command_t
    """
    Eliminate redundant subshells like ( echo hi ) | wc -l etc.
    """
    UP_node = node
    with tagswitch(node) as case:
      if case(command_e.Subshell):
        node = cast(command__Subshell, UP_node)
        if len(node.redirects) == 0:
          # Note: technically we could optimize this into BraceGroup with
          # redirects.  Some shells appear to do that.
          if 0:
            log('removing subshell')
          # Optimize ( ( date ) ) etc.
          return self._RemoveSubshells(node.child)
    return node

  def ExecuteAndCatch(self, node, cmd_flags=0):
    # type: (command_t, int) -> Tuple[bool, bool]
    """Execute a subprogram, handling _ControlFlow and fatal exceptions.

    Args:
      node: LST subtree
      optimize: Whether to exec the last process rather than fork/exec

    Returns:
      TODO: use enum 'why' instead of the 2 booleans

    Used by
    - main_loop.py.
    - SubProgramThunk for pipelines, subshell, command sub, process sub
    - TODO: Signals besides EXIT trap

    Note: To do what optimize does, dash has EV_EXIT flag and yash has a
    finally_exit boolean.  We use a different algorithm.
    """
    if cmd_flags & Optimize:
      node = self._RemoveSubshells(node)
      self._NoForkLast(node)  # turn the last ones into exec

    if 0:
      log('after opt:')
      node.PrettyPrint()
      log('')

    is_return = False
    is_fatal = False
    is_errexit = False

    err = None  # type: error._ErrorWithLocation

    try:
      status = self._Execute(node)
    except _ControlFlow as e:
      if cmd_flags & RaiseControlFlow:
        raise  # 'eval break' and 'source return.sh', etc.
      else:
        # Return at top level is OK, unlike in bash.
        if e.IsReturn():
          is_return = True
          status = e.StatusCode()
        else:
          # TODO: This error message is invalid.  Can also happen in eval.
          # We need a flag.

          # Invalid control flow
          self.errfmt.Print_(
              "Loop and control flow can't be in different processes",
              span_id=e.token.span_id)
          is_fatal = True
          # All shells exit 0 here.  It could be hidden behind
          # strict_control_flow if the incompatibility causes problems.
          status = 1
    except error.Parse as e:
      self.dumper.MaybeCollect(self, e)  # Do this before unwinding stack
      raise
    except error.ErrExit as e:
      err = e
      is_errexit = True
    except error.FatalRuntime as e:
      err = e

    if err:
      status = err.ExitStatus()

      is_fatal = True
      self.dumper.MaybeCollect(self, err)  # Do this before unwinding stack

      if not err.HasLocation():  # Last resort!
        err.span_id = self.mem.CurrentSpanId()

      if is_errexit and not self.exec_opts.verbose_errexit():
        pass  # Suppress error
      else:
        ui.PrettyPrintError(err, self.arena, prefix='fatal: ')

    # Problem: We have no idea here if a SUBSHELL (or pipeline comment) already
    # created a crash dump.  So we get 2 or more of them.
    self.dumper.MaybeDump(status)

    self.mem.SetLastStatus(status)
    return is_return, is_fatal

  def MaybeRunExitTrap(self, mut_status):
    # type: (List[int]) -> None
    """If an EXIT trap exists, run it.
    
    Only mutates the status if 'return' or 'exit'.  This is odd behavior, but
    all bash/dash/mksh seem to agree on it.  See cases 7-10 in
    builtin-trap.test.sh.

    Note: if we could easily modulo -1 % 256 == 255 here, then we could get rid
    of this awkward interface.  But that's true in Python and not C!

    Could use i & (n-1) == i & 255  because we have a power of 2.
    https://stackoverflow.com/questions/14997165/fastest-way-to-get-a-positive-modulo-in-c-c
    """
    handler = self.traps.get('EXIT')
    if handler:
      with dev.ctx_Tracer(self.tracer, 'trap EXIT', None):
        try:
          is_return, is_fatal = self.ExecuteAndCatch(handler.node)
        except util.UserExit as e:  # explicit exit
          mut_status[0] = e.status
          return
        if is_return:  # explicit 'return' in the trap handler!
          mut_status[0] = self.LastStatus()

  def RunProc(self, proc, argv, arg0_spid):
    # type: (Proc, List[str], int) -> int
    """Run a shell "functions".

    For SimpleCommand and registered completion hooks.
    """
    sig = proc.sig
    if sig.tag_() == proc_sig_e.Closed:
      # We're binding named params.  User should use @rest.  No 'shift'.
      proc_argv = []  # type: List[str]
    else:
      proc_argv = argv

    with state.ctx_Call(self.mem, self.mutable_opts, proc, proc_argv):
      n_args = len(argv)
      UP_sig = sig

      if UP_sig.tag_() == proc_sig_e.Closed:  # proc is-closed ()
        sig = cast(proc_sig__Closed, UP_sig)
        for i, p in enumerate(sig.untyped):
          is_out_param = p.ref is not None

          param_name = p.name.val
          if i < n_args:
            arg_str = argv[i]

            # If we have myproc(p), and call it with myproc :arg, then bind
            # __p to 'arg'.  That is, the param has a prefix ADDED, and the arg
            # has a prefix REMOVED.
            #
            # This helps eliminate "nameref cycles".
            if is_out_param:
              param_name = '__' + param_name

              if not arg_str.startswith(':'):
                # TODO: Point to the exact argument
                e_die('Invalid argument %r.  Expected a name starting with :',
                      arg_str)
              arg_str = arg_str[1:]

            val = value.Str(arg_str)  # type: value_t
          else:
            val = proc.defaults[i]
            if val is None:
              e_die("No value provided for param %r", p.name.val)

          if is_out_param:
            flags = state.SetNameref 
          else:
            flags = 0

          self.mem.SetValue(lvalue.Named(param_name), val, scope_e.LocalOnly,
                            flags=flags)

        n_params = len(sig.untyped)
        if sig.rest:
          leftover = value.MaybeStrArray(argv[n_params:])
          self.mem.SetValue(
              lvalue.Named(sig.rest.val), leftover, scope_e.LocalOnly)
        else:
          if n_args > n_params:
            self.errfmt.Print_(
                "proc %r expected %d arguments, but got %d" %
                (proc.name, n_params, n_args), span_id=arg0_spid)
            # This should be status 2 because it's like a usage error.
            return 2

      # Redirects still valid for functions.
      # Here doc causes a pipe and Process(SubProgramThunk).
      try:
        status = self._Execute(proc.body)
      except _ControlFlow as e:
        if e.IsReturn():
          status = e.StatusCode()
        else:
          # break/continue used in the wrong place.
          e_die('Unexpected %r (in function call)', e.token.val, token=e.token)
      except error.FatalRuntime as e:
        # Dump the stack before unwinding it
        self.dumper.MaybeCollect(self, e)
        raise

    return status

  def EvalBlock(self, block):
    # type: (command_t) -> Dict[str, cell]
    """
    Returns a namespace.  For config files.

    rule foo {
      a = 1
    }
    is like:
    foo = {a:1}

    """
    status = 0
    namespace_ = None  # type: Dict[str, cell]
    try:
      self._Execute(block)  # can raise FatalRuntimeError, etc.
    except _ControlFlow as e:  # A block is more like a function.
      if e.IsReturn():
        status = e.StatusCode()
      else:
        raise
    finally:
      namespace_ = self.mem.TopNamespace()

    # This is the thing on self.mem?
    # Filter out everything beginning with _ ?

    # TODO: Return arbitrary values instead

    # Nothing seems to depend on this, and mypy isn't happy with it
    # because it's an int and values of the namespace dict should be
    # cells, so I've commented it out.
    #namespace['_returned'] = status
    return namespace_

  def RunFuncForCompletion(self, proc, argv):
    # type: (Proc, List[str]) -> int
    # TODO: Change this to run Oil procs and funcs too
    try:
      status = self.RunProc(proc, argv, runtime.NO_SPID)
    except error.FatalRuntime as e:
      ui.PrettyPrintError(e, self.arena)
      status = e.ExitStatus()
    except _ControlFlow as e:
      # shouldn't be able to exit the shell from a completion hook!
      # TODO: Avoid overwriting the prompt!
      self.errfmt.Print_('Attempted to exit from completion hook.',
                         span_id=e.token.span_id)

      status = 1
    # NOTE: (IOError, OSError) are caught in completion.py:ReadlineCallback
    return status
