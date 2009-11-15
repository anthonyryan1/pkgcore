# Copyright: 2006-2009 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD

import operator
from itertools import chain, islice, ifilterfalse as filterfalse
from collections import deque

from pkgcore.resolver.choice_point import choice_point
from pkgcore.restrictions import packages, values, restriction
from pkgcore.repository.misc import caching_repo
from pkgcore.repository.visibility import filterTree
from pkgcore.resolver import state

from snakeoil.currying import partial, post_curry
from snakeoil.compatibility import any, cmp, sort_cmp
from snakeoil.iterables import caching_iter, iter_sort


limiters = set(["cycle"])#, None])
def dprint(fmt, args=None, label=None):
    if None in limiters or label in limiters:
        if args is None:
            print fmt
        else:
            print fmt % args


#iter/pkg sorting functions for selection strategy
pkg_sort_highest = partial(sorted, reverse=True)
pkg_sort_lowest = sorted

pkg_grabber = operator.itemgetter(0)

def highest_iter_sort(l, pkg_grabber=pkg_grabber):
    def f(x, y):
        c = cmp(x, y)
        if c:
            return c
        elif x.repo.livefs:
            if y.repo.livefs:
                return 0
            return 1
        elif y.repo.livefs:
            return -1
        return 0
    sort_cmp(l, f, key=pkg_grabber, reverse=True)
    return l


def lowest_iter_sort(l, pkg_grabber=pkg_grabber):
    def f(x, y):
        c = cmp(x, y)
        if c:
            return c
        elif x.repo.livefs:
            if y.repo.livefs:
                return 0
            return -1
        elif y.repo.livefs:
            return 1
        return 0
    sort_cmp(l, f, key=pkg_grabber)
    return l


class MutableContainmentRestriction(values.base):

    __slots__ = ('_blacklist', 'match')

    def __init__(self, blacklist):
        sf = object.__setattr__
        sf(self, '_blacklist', blacklist)
        sf(self, 'match', self._blacklist.__contains__)


class resolver_frame(object):

    __slots__ = ("parent", "atom", "choices", "mode", "start_point", "dbs",
        "depth", "drop_cycles", "__weakref__", "ignored", "vdb_limited",
        "events", "succeeded")

    def __init__(self, parent, mode, atom, choices, dbs, start_point, depth,
        drop_cycles, ignored=False, vdb_limited=False):
        self.parent = parent
        self.atom = atom
        self.choices = choices
        self.dbs = dbs
        self.mode = mode
        self.start_point = start_point
        self.depth = depth
        self.drop_cycles = drop_cycles
        self.ignored = False
        self.vdb_limited = vdb_limited
        self.events = []
        self.succeeded = None

    def reduce_solutions(self, nodes):
        if isinstance(nodes, (list, tuple)):
            self.events.append(("reduce", nodes))
        else:
            self.events.append(("reduce", (nodes,)))
        return self.choices.reduce_atoms(nodes)

    def __str__(self):
        pkg = self.current_pkg
        if pkg is None:
            pkg = "exhausted"
        else:
            cpv = pkg.cpvstr
            pkg = getattr(pkg.repo, 'repo_id', None)
            if pkg is not None:
                pkg = "%s::%s" % (cpv, pkg)
            else:
                pkg = str(pkg)
        if self.succeeded is not None:
            result = ": %s" % (self.succeeded and "succeeded" or "failed")
        else:
            result = ""
        return "frame%s: mode %r: atom %s: current %s%s%s%s" % \
            (result, self.mode, self.atom, pkg,
            self.drop_cycles and ": cycle dropping" or '',
            self.ignored and ": ignored" or '',
            self.vdb_limited and ": vdb limited" or '')

    @property
    def current_pkg(self):
        try:
            return self.choices.current_pkg
        except IndexError:
            return None


class resolver_stack(deque):

    frame_klass = resolver_frame
    depth = property(len)
    current_frame = property(operator.itemgetter(-1))
    filter_ignored = staticmethod(
        partial(filterfalse, operator.attrgetter("ignored")))

    # this *has* to be a property, else it creates a cycle.
    parent = property(lambda s:s)

    def __init__(self):
        self.events = []

    def __str__(self):
        return 'resolver stack:\n  %s' % '\n  '.join(str(x) for x in self)

    def __repr__(self):
        return '<%s: %r>' % (self.__class__.__name__,
            tuple(repr(x) for x in self))

    def add_frame(self, mode, atom, choices, dbs, start_point, drop_cycles, vdb_limited=False):
        if not self:
            parent = self
        else:
            parent = self[-1]
        frame = self.frame_klass(parent, mode, atom, choices, dbs, start_point,
            self.depth + 1, drop_cycles, vdb_limited=vdb_limited)
        self.append(frame)
        return frame

    def add_event(self, event):
        if not self:
            self.events.append(event)
        else:
            self[-1].events.append(event)

    def pop_frame(self, result):
        frame = self.pop()
        frame.succeeded = bool(result)
        frame.parent.events.append(frame)

    def will_cycle(self, atom, cur_choice, attr, start=0):
        # short cut...
        if attr == "post_rdepends":
            # not possible for a cycle we'll care about to exist.
            # the 'cut off' point is for the new atom, thus not possible for
            # a cycle.
            return -1

        cycle_start = -1
        if start != 0:
            i = islice(self, start, None)
        else:
            i = self
        for idx, x in enumerate(i):
            if x.mode == "post_rdepends":
                cycle_start = -1
            if x.atom == atom:
                cycle_start = idx

        if cycle_start != -1:
            # deque can't be sliced, thus islice
            if attr is not None:
                s = ', '.join('[%s: %s]' % (x.atom, x.current_pkg) for x in
                    islice(self, cycle_start))
                if s:
                    s += ', '
                s += '[%s: %s]' % (atom, cur_choice.current_pkg)
                dprint("%s level cycle: stack: %s\n",
                    (attr, s), "cycle")
            return cycle_start + start
        return -1

    def pkg_cycles(self, trg_frame, **kwds):
        pkg = trg_frame
        return (frame for frame in self._cycles(trg_frame, skip_trg_frame=True,
            **kwds)
            if pkg == frame.current_pkg)

    def atom_cycles(self, trg_frame, **kwds):
        atom = trg_frame.atom
        return (frame for frame in self._cycles(trg_frame, skip_trg_frame=True,
            **kwds)
            if atom == frame.atom)

    def slot_cycles(self, trg_frame, **kwds):
        pkg = trg_frame.current_pkg
        slot = pkg.slot
        key = pkg.key
        return (frame for frame in self._cycles(trg_frame, skip_trg_frame=True,
            **kwds)
            if key == frame.current_pkg.key and slot == frame.current_pkg.slot)

    def _cycles(self, trg_frame, start=0, reverse=False, skip_trg_frame=True):
        i = self.filter_ignored(self)
        if reverse:
            i = self.filter_ignored(reversed(self))
        else:
            i = self.filter_ignored(self)
        if start != 0:
            i = islice(i, start, None)
        if skip_trg_frame:
            return (frame for frame in i if frame is not trg_frame)
        return i

    def index(self, frame, start=0, stop=None):
        if start != 0 or stop is not None:
            i = slice(self, start, stop)
        else:
            i = self
        for idx, x in enumerate(self):
            if x == frame:
                return idx + start
        return -1


class merge_plan(object):

    vdb_restrict = packages.PackageRestriction("repo.livefs",
        values.EqualityMatch(True))

    def __init__(self, dbs, per_repo_strategy,
                 global_strategy=None,
                 depset_reorder_strategy=None,
                 process_built_depends=False,
                 drop_cycles=False, debug=False):

        if not isinstance(dbs, (list, tuple)):
            dbs = [dbs]

        if global_strategy is None:
            global_strategy = self.default_global_strategy

        if depset_reorder_strategy is None:
            depset_reorder_strategy = self.default_depset_reorder_strategy

        self.depset_reorder = depset_reorder_strategy
        self.per_repo_strategy = per_repo_strategy
        self.global_strategy = global_strategy
        self.forced_atoms = set()
        self.all_dbs = [caching_repo(x, self.per_repo_strategy) for x in dbs]
        self.state = state.plan_state()
        vdb_state_filter_restrict = MutableContainmentRestriction(self.state.vdb_filter)
        self.livefs_dbs = [filterTree(x, vdb_state_filter_restrict) for x in self.all_dbs if x.livefs]
        self.dbs = [x for x in self.all_dbs if not x.livefs]
        self.insoluble = set()
        self.vdb_preloaded = False
        self.drop_cycles = drop_cycles
        self.process_built_depends = process_built_depends
        if debug:
            self._rec_add_atom = partial(self._stack_debugging_rec_add_atom,
                self._rec_add_atom)
            self._debugging_depth = 0
            self._debugging_drop_cycles = False

    def notify_starting_mode(self, mode, stack):
        if mode == "post_rdepends":
            mode = 'prdepends'
        dprint("%s:%s%s: started: %s" %
            (mode, ' ' * ((stack.current_frame.depth * 2) + 12 - len(mode)),
                stack.current_frame.atom,
                stack.current_frame.choices.current_pkg)
            )

    def notify_trying_choice(self, stack, atom, choices):
        dprint("choose for %s%s, %s",
               (stack.depth *2*" ", atom, choices.current_pkg))
        stack.add_event(('inspecting', choices.current_pkg))

    def notify_choice_failed(self, stack, atom, choices, msg, msg_args=()):
        stack[-1].events.append(("choice", str(choices.current_pkg), False, msg % msg_args))
        if msg:
            msg = ': %s' % (msg % msg_args)
        dprint("choice for %s%s, %s failed%s",
            (stack.depth * 2 * ' ', atom, choices.current_pkg, msg))

    def notify_choice_succeeded(self, stack, atom, choices, msg='', msg_args=()):
        stack[-1].events.append(("choice", str(choices.current_pkg), True, msg))
        if msg:
            msg = ': %s' % (msg % msg_args)
        dprint("choice for %s%s, %s succeeded%s",
            (stack.depth * 2 * ' ', atom, choices.current_pkg, msg))

    def notify_viable(self, stack, atom, viable, msg='', pre_solved=False):
        t_viable = viable and "processing" or "not viable"
        if pre_solved and viable:
            t_viable = "pre-solved"
        t_msg = msg and (" "+msg) or ''
        s=''
        if stack:
            s = " for %s " % (stack[-1].atom)
        dprint("%s%s%s%s%s", (t_viable.ljust(13), "  "*stack.depth, atom, s, t_msg))
        stack.add_event(("viable", viable, pre_solved, atom, msg))

    def load_vdb_state(self):
        for r in self.livefs_dbs:
            for pkg in r.__db__:
                dprint("inserting %s from %s", (pkg, r), "vdb")
                ret = self.add_atom(pkg.versioned_atom, dbs=self.livefs_dbs)
                dprint("insertion of %s from %s: %s", (pkg, r, ret), "vdb")
                if ret:
                    raise Exception(
                        "couldn't load vdb state, %s %s" %
                        (pkg.versioned_atom, ret))
        self.vdb_preloaded = True

    def add_atom(self, atom, dbs=None):
        """add an atom, recalculating as necessary.

        @return: the last unresolvable atom stack if a solution can't be found,
            else returns None if the atom was successfully added.
        """
        if dbs is None:
            dbs = self.all_dbs
        if atom not in self.forced_atoms:
            stack = resolver_stack()
            ret = self._rec_add_atom(atom, stack, dbs)
            if ret:
                dprint("failed- %s", ret)
                return ret, stack.events[0]
            else:
                self.forced_atoms.add(atom)

        return ()

    def _stack_debugging_rec_add_atom(self, func, atom, stack, dbs, **kwds):
        current = len(stack)
        cycles = kwds.get('drop_cycles', False)
        reset_cycles = False
        if cycles and not self._debugging_drop_cycles:
            self._debugging_drop_cycles = reset_cycles = True
        if not reset_cycles:
            self._debugging_depth += 1

        assert current == self._debugging_depth -1
        ret = func(atom, stack, dbs, **kwds)
        assert current == len(stack)
        assert current == self._debugging_depth -1
        if not reset_cycles:
            self._debugging_depth -= 1
        else:
            self._debugging_drop_cycles = False
        return ret

    def _rec_add_atom(self, atom, stack, dbs, mode="none", drop_cycles=False):
        """Add an atom.

        @return: False on no issues (inserted succesfully),
            else a list of the stack that screwed it up.
        """
        limit_to_vdb = dbs == self.livefs_dbs

        depth = stack.depth

        matches = self._viable(stack, mode, atom, dbs, drop_cycles, limit_to_vdb)
        if matches is None:
            stack.pop_frame(False)
            return [atom]
        elif matches is True:
            stack.pop_frame(True)
            return None
        choices, matches = matches

        if stack:
            if limit_to_vdb:
                dprint("processing   %s%s  [%s]; mode %s vdb bound",
                       (depth*2*" ", atom, stack[-1].atom, mode))
            else:
                dprint("processing   %s%s  [%s]; mode %s",
                       (depth*2*" ", atom, stack[-1].atom, mode))
        else:
            dprint("processing   %s%s", (depth*2*" ", atom))

        ret = self.check_for_cycles(stack, stack.current_frame)
        if ret is not True:
            stack.pop_frame(ret is None)
            return ret

        blocks = []
        failures = []

        last_state = None
        while choices:
            new_state = choices.state
            if last_state == new_state:
                raise AssertionError("no state change detected, "
                    "old %r != new %r\nchoices(%r)\ncurrent(%r)\ndepends(%r)\n"
                    "rdepends(%r)\npost_rdepends(%r)\nprovides(%r)" %
                    (last_state, new_state, tuple(choices.matches),
                        choices.current_pkg, choices.depends,
                        choices.rdepends, choices.post_rdepends,
                        choices.provides))
            last_state = new_state
            additions, blocks = [], []

            self.notify_trying_choice(stack, atom, choices)

            if not choices.current_pkg.built or self.process_built_depends:
                l = self.process_dependencies(stack, "depends",
                    self.depset_reorder(self, choices.depends, "depends"))
                if len(l) == 1:
                    dprint("reseting for %s%s because of depends: %s",
                           (depth*2*" ", atom, l[0][-1]))
                    self.state.backtrack(stack.current_frame.start_point)
                    failures = l[0]
                    continue
                additions += l[0]
                blocks = l[1]

                # level blockers.
                ret = self.insert_blockers(stack, choices, blocks)
                if ret is not None:
                    # hackish in terms of failures, needs cleanup
                    failures = [ret[0]]
                    self.notify_choice_failed(stack, atom, choices,
                        "depends blocker: %s conflicts w/ %s", (ret[0], ret[1]))
                    stack.current_frame.reduce_solutions(ret[0])
                    self.state.backtrack(stack.current_frame.start_point)
                    continue

            l = self.process_dependencies(stack, "rdepends",
                self.depset_reorder(self, choices.rdepends, "rdepends"))
            if len(l) == 1:
                dprint("reseting for %s%s because of rdepends: %s",
                       (depth*2*" ", atom, l[0]))
                self.state.backtrack(stack.current_frame.start_point)
                failures = l[0]
                continue
            additions += l[0]
            blocks = l[1]

            ret = self.insert_blockers(stack, choices, blocks)
            if ret is not None:
                # hackish in terms of failures, needs cleanup
                failures = [ret[0]]
                self.notify_choice_failed(stack, atom, choices,
                    "rdepends blocker: %s conflicts w/ %s", (ret[0], ret[1]))
                stack.current_frame.reduce_solutions(ret[0])
                self.state.backtrack(stack.current_frame.start_point)
                continue

            l = self.insert_choice(atom, stack, choices)
            if l is False:
                # this means somehow the node already slipped in.
                # so we exit now, we are satisfied
                self.notify_choice_succeeded(stack, atom, choices,
                    "already exists in the state plan")
                stack.pop_frame(True)
                return None
            elif l is not None:
                # failure.
                self.notify_choice_failed(stack, atom, choices,
                    "failed inserting: %s", l)
                self.state.backtrack(stack.current_frame.start_point)
                choices.force_next_pkg()
                continue

            fail = self.insert_providers(stack, atom, choices)
            if fail:
                self.state.backtrack(stack.current_frame.start_point)
                choices.force_next_pkg()
                continue

            l = self.process_dependencies(stack, "post_rdepends",
                self.depset_reorder(self, choices.post_rdepends,
                                    "post_rdepends"))

            if len(l) == 1:
                dprint("resetting for %s%s because of rdepends: %s",
                       (depth*2*" ", atom, l[0]))
                self.state.backtrack(stack.current_frame.start_point)
                failures = l[0]
                continue
            additions += l[0]
            blocks = l[1]

            # level blockers.
            ret = self.insert_blockers(stack, choices, blocks)
            if ret is not None:
                # hackish in terms of failures, needs cleanup
                failures = [ret[0]]
                self.notify_choice_failed(stack, atom, choices,
                    "pdepends blocker: %s conflicts w/ %s", (ret[0], ret[1]))
                stack.current_frame.reduce_solutions(ret[0])
                self.state.backtrack(stack.current_frame.start_point)
                continue
            # kinky... the request is fully satisfied
            break

        else:
            dprint("no solution  %s%s", (depth*2*" ", atom))
            stack.add_event(("debug", "ran out of choices",))
            self.state.backtrack(stack.current_frame.start_point)
            # saving roll.  if we're allowed to drop cycles, try it again.
            # this needs to be *far* more fine grained also. it'll try
            # regardless of if it's cycle issue
            if not drop_cycles and self.drop_cycles:
                stack.add_event(("cycle", cur_frame, "trying to drop any cycles"),)
                dprint("trying saving throw for %s ignoring cycles",
                       atom, "cycle")
                # note everything is retored to a pristine state prior also.
                stack[-1].ignored = True
                l = self._rec_add_atom(atom, stack, dbs,
                    mode=mode, drop_cycles=True)
                if not l:
                    stack.pop_frame(True)
                    return None
            stack.pop_frame(False)
            return [atom] + failures

        self.notify_choice_succeeded(stack, atom, choices)
        stack.pop_frame(True)
        return None

    def insert_providers(self, stack, atom, choices):
        for x in choices.provides:
            l = state.add_op(choices, x).apply(self.state)
            if l and l != [x]:
                # slight hack; basically, should be pruning providers as the parent is removed
                # this duplicates it, basically; if it's not a restrict, then it's a pkg.
                # thus poke it.
                if len(l) == 1 and not isinstance(l[0], restriction.base):
                    p = getattr(l[0], 'provider', None)
                    if p is not None and not self.state.match_atom(p):
                        # ok... force it.
                        fail = state.replace_op(choices, x).apply(self.state)
                        if not fail:
                            continue
                        self.notify_choice_failed(stack, atom, choices,
                            "failed forcing provider: %s due to conflict %s", (x, p))
                        return fail
                self.notify_choice_failed(stack, atom, choices,
                    "failed inserting provider: %s due to conflict %s", (x, l))
                return l
        return None

    def _viable(self, stack, mode, atom, dbs, drop_cycles, limit_to_vdb):
        """
        internal function to discern if an atom is viable, returning
        the choicepoint/matches iter if viable.

        @return: 3 possible; None (not viable), True (presolved),
          L{caching_iter} (not solved, but viable), L{choice_point}
        """
        choices = ret = None
        if atom in self.insoluble:
            ret = ((False, "globally insoluable"),{})
            matches = ()
        else:
            matches = self.state.match_atom(atom)
            if matches:
                ret = ((True,), {"pre_solved":True})
            else:
                # not in the plan thus far.
                matches = caching_iter(self.global_strategy(self, dbs, atom))
                if matches:
                    choices = choice_point(atom, matches)
                    # ignore what dropped out, at this juncture we don't care.
                    choices.reduce_atoms(self.insoluble)
                    if not choices:
                        # and was intractable because it has a hard dep on an
                        # unsolvable atom.
                        ret = ((False, "pruning of insoluable deps "
                            "left no choices"), {})
#                    else:
#                    self.notify_viable(stack, atom, False,
#                        msg="pruning of insoluble deps left no choices")
                else:
                    ret = ((False, "no matches"), {})

        if choices is None:
            choices = choice_point(atom, matches)

        stack.add_frame(mode, atom, choices, dbs,
            self.state.current_state, drop_cycles, vdb_limited=limit_to_vdb)

        if not limit_to_vdb and not matches:
            self.insoluble.add(atom)
        if ret is not None:
            self.notify_viable(stack, atom, *ret[0], **ret[1])
            if ret[0][0] == True:
                return True
            return None
        return choices, matches

    def check_for_cycles(self, stack, cur_frame):
        """check the current stack for cyclical issues;
        @param stack: current stack, a L{resolver_stack} instance
        @param cur_frame: current frame, a L{resolver_frame} instance
        @return: True if no issues and resolution should continue, else the
            value to return after collapsing the calling frame
        """
        force_vdb = False
        for frame in stack.slot_cycles(cur_frame, reverse=True):
            if not any(f.mode == 'post_rdepends' for f in
                islice(stack, stack.index(frame), stack.index(cur_frame))):
                # exact same pkg.
                if frame.mode == 'depends':
                    # ok, we *must* go vdb if not already.
                    if frame.current_pkg.repo.livefs:
                        if cur_frame.current_pkg.repo.livefs:
                            return None
                        # force it to vdb.
                    if cur_frame.current_pkg.repo.livefs:
                        return True
                    elif cur_frame.current_pkg == frame.current_pkg and \
                        cur_frame.mode == 'post_rdepends':
                        # if non vdb and it's a post_rdeps cycle for the cur
                        # node, exempt it; assuming the stack succeeds,
                        # it's satisfied
                        return True
                    force_vdb = True
                    break
                else:
                    # should be doing a full walk of the cycle here, seeing
                    # if an rdep becomes a dep.
                    return None
                # portage::gentoo -> rysnc -> portage::vdb; let it process it.
                return True
            # only need to look at the most recent match; reasoning is simple,
            # logic above forces it to vdb if needed.
            break
        if not force_vdb:
            return True
        # we already know the current pkg isn't livefs; force livefs to
        # sidestep this.
        cur_frame.parent.events.append(("cycle", cur_frame, "limiting to vdb"))
        cur_frame.ignored = True
        return self._rec_add_atom(cur_frame.atom, stack,
            self.livefs_dbs, mode=cur_frame.mode,
            drop_cycles = cur_frame.drop_cycles)

    def process_dependencies(self, stack, attr, depset):
        failure = []
        additions, blocks, = [], []
        cur_frame = stack.current_frame
        self.notify_starting_mode(attr, stack)
        for potentials in depset:
            failure = []
            for or_node in potentials:
                if or_node.blocks:
                    blocks.append(or_node)
                    break
                failure = self._rec_add_atom(or_node, stack,
                    cur_frame.dbs, mode=attr,
                    drop_cycles=cur_frame.drop_cycles)
                if failure:
                    # XXX this is whacky tacky fantastically crappy
                    # XXX kill it; purpose seems... questionable.
                    if failure and cur_frame.drop_cycles:
                        dprint("%s level cycle: %s: "
                               "dropping cycle for %s from %s",
                                (attr, cur_frame.atom, or_node,
                                 cur_frame.current_pkg),
                                "cycle")
                        failure = None
                        break

                    if cur_frame.reduce_solutions(or_node):
                        # pkg changed.
                        return [failure]
                    continue
                additions.append(or_node)
                break
            else: # didn't find any solutions to this or block.
                cur_frame.reduce_solutions(potentials)
                return [potentials]
        else: # all potentials were usable.
            return additions, blocks

    def insert_choice(self, atom, stack, choices):
        # well, we got ourselvs a resolution.
        # do a trick to make the resolver now aware of vdb pkgs if needed
        if not self.vdb_preloaded and not choices.current_pkg.repo.livefs:
            slotted_atom = choices.current_pkg.slotted_atom
            l = self.state.match_atom(slotted_atom)
            if not l:
                # hmm. ok... no conflicts, so we insert in vdb matches
                # to trigger a replace instead of an install
                for repo in self.livefs_dbs:
                    m = repo.match(slotted_atom)
                    if m:
                        c = choice_point(slotted_atom, m)
                        state.add_op(c, c.current_pkg, force=True).apply(self.state)
                        break

        # first, check for conflicts.
        # lil bit fugly, but works for the moment
        conflicts = state.add_op(choices, choices.current_pkg).apply(self.state)
        if conflicts:
            # this means in this branch of resolution, someone slipped
            # something in already. cycle, basically.
            # hack.  see if what was insert is enough for us.

            # this is tricky... if it's the same node inserted
            # (cycle), then we ignore it; this does *not* perfectly
            # behave though, doesn't discern between repos.

            if (len(conflicts) == 1 and conflicts[0] == choices.current_pkg and
                conflicts[0].repo.livefs == choices.current_pkg.repo.livefs and
                atom.match(conflicts[0])):

                # early exit. means that a cycle came about, but exact
                # same result slipped through.
                return False

            dprint("was trying to insert atom '%s' pkg '%s',\n"
                   "but '[%s]' exists already",
                   (atom, choices.current_pkg,
                   ", ".join(map(str, conflicts))))

            try_rematch = False
            if any(True for x in conflicts if isinstance(x, restriction.base)):
                # blocker was caught
                try_rematch = True
            elif not any(True for x in conflicts if not
                self.vdb_restrict.match(x)):
                # vdb entry, replace.
                if self.vdb_restrict.match(choices.current_pkg):
                    # we're replacing a vdb entry with a vdb entry?  wtf.
                    print ("internal weirdness spotted- vdb restrict matches, "
                        "but current doesn't, bailing")
                    raise Exception()
                conflicts = state.replace_op(choices, choices.current_pkg).apply(
                    self.state)
                if not conflicts:
                    dprint("replacing vdb entry for   '%s' with pkg '%s'",
                        (atom, choices.current_pkg))

            else:
                try_rematch = True
            if try_rematch:
                # XXX: this block looks whacked.  figure out what it's up to.
                l2 = self.state.match_atom(atom)
                if l2 == [choices.current_pkg]:
                    # stop resolution.
                    conflicts = False
                elif l2:
                    # potentially need to do some form of cleanup here.
                    conflicts = False
        else:
            conflicts = None
        return conflicts

    def generate_mangled_blocker(self, choices, blocker):
        """converts a blocker into a "cannot block ourself" block"""
        # note the second Or clause is a bit loose; allows any version to
        # slip through instead of blocking everything that isn't the
        # parent pkg
        if blocker.category != 'virtual':
            return blocker
        return packages.AndRestriction(blocker,
            packages.PackageRestriction("provider.key",
                values.StrExactMatch(choices.current_pkg.key),
                negate=True, ignore_missing=True),
            finalize=True)

    def insert_blockers(self, stack, choices, blocks):
        # level blockers.
        for x in blocks:
            # check for any matches; none, try and insert vdb nodes.
            if not self.vdb_preloaded and \
                not choices.current_pkg.repo.livefs:
                matches = self.state.match_atom(x)
                # if it's a virtual, we only check the first- >1 matches
                # means that vdb was loaded already.
                # also uses getattr to protect against it *not* being
                # a virtual provider.
                if not matches:#
                    for repo in self.livefs_dbs:
                        m = repo.match(x)
                        if m:
                            dprint("inserting vdb node for blocker"
                                " %s %s" % (x, m[0]))
                            # ignore blockers for for vdb atm, since
                            # when we level this nodes blockers they'll
                            # hit
                            c = choice_point(x, m)
                            state.add_op(c, c.current_pkg, force=True).apply(
                                self.state)
                            break

            rewrote_blocker = self.generate_mangled_blocker(choices, x)
            l = self.state.add_blocker(choices, rewrote_blocker, key=x.key)
            if l:
                # blocker caught something. yay.
                dprint("%s blocker %s hit %s for atom %s pkg %s",
                       (stack[-1].mode, x, l, stack[-1].atom, choices.current_pkg))
                return x, l
        return None

    def free_caches(self):
        for repo in self.all_dbs:
            repo.clear()

    # selection strategies for atom matches

    @staticmethod
    def default_depset_reorder_strategy(self, depset, mode):
        for or_block in depset:
            vdb = []
            non_vdb = []
            if len(or_block) == 1:
                yield or_block
                continue
            for atom in or_block:
                if atom.blocks:
                    non_vdb.append(atom)
                elif self.state.match_atom(atom):
                    vdb.append(atom)
                elif any(True for r in self.livefs_dbs
                    for p in r.match(atom)):
                    vdb.append(atom)
                else:
                    non_vdb.append(atom)
            if vdb:
                yield vdb + non_vdb
            else:
                yield or_block

    @staticmethod
    def default_global_strategy(self, dbs, atom):
        return chain(*[repo.match(atom) for repo in dbs])

    @staticmethod
    def just_livefs_dbs(dbs):
        return (r for r in dbs if r.livefs)

    @staticmethod
    def just_nonlivefs_dbs(dbs):
        return (r for r in dbs if not r.livefs)

    @classmethod
    def prefer_livefs_dbs(cls, dbs, just_vdb=None):
        """
        @param dbs: db list to walk
        @param just_vdb: if None, no filtering; if True, just vdb, if False,
          non-vdb only
        @return: yields repositories in requested ordering
        """
        return chain(cls.just_livefs_dbs(dbs), cls.just_nonlivefs_dbs(dbs))

    @staticmethod
    def prefer_highest_version_strategy(self, dbs, atom):
        # XXX rework caching_iter so that it iter's properly
        return iter_sort(highest_iter_sort,
                         *[repo.match(atom)
                         for repo in self.prefer_livefs_dbs(dbs)])

    @staticmethod
    def prefer_lowest_version_strategy(self, dbs, atom):
        return iter_sort(lowest_iter_sort,
                         self.default_global_strategy(self, dbs, atom))

    @staticmethod
    def prefer_reuse_strategy(self, dbs, atom):

        return chain(
            iter_sort(highest_iter_sort,
                *[repo.match(atom) for repo in self.just_livefs_dbs(dbs)]),
            iter_sort(highest_iter_sort,
                *[repo.match(atom) for repo in self.just_nonlivefs_dbs(dbs)])
        )

    def generic_force_version_strategy(self, vdb, dbs, atom, iter_sorter,
                                       pkg_sorter):
        try:
            # nasty, but works.
            yield iter_sort(iter_sorter,
                            *[r.itermatch(atom, sorter=pkg_sorter)
                              for r in [vdb] + dbs]).next()
        except StopIteration:
            # twas no matches
            pass

    force_max_version_strategy = staticmethod(
        post_curry(generic_force_version_strategy,
                   highest_iter_sort, pkg_sort_highest))
    force_min_version_strategy = staticmethod(
        post_curry(generic_force_version_strategy,
                   lowest_iter_sort, pkg_sort_lowest))
