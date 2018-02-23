import re
from numbers import Number

from SCons.Util import is_String, is_Sequence, CLVar
from SCons.Subst import CmdStringHolder, create_subst_target_source_dict, AllowableExceptions, raise_exception
from SCons.EnvironmentValue import EnvironmentValue, ValueTypes, separate_args, SubstModes

# _is_valid_var = re.compile(r'[_a-zA-Z]\w*$')
#
# _rm = re.compile(r'\$[()]')
# _remove = re.compile(r'\$\([^\$]*(\$[^\)][^\$]*)*\$\)')

_debug = True
if _debug:
    def debug(fmt, *args):
        # format when needed
        print(fmt % args)
else:
    # avoid any formatting overhead
    def debug(*unused):
        pass


class EnvironmentValues(object):
    """
    A class to hold all the environment variables
    """

    def __init__(self, **kw):
        self.values = dict((k, EnvironmentValue(v)) for k, v in kw.items())
        self._dict = kw.copy()
        self.key_set = set(kw.keys())

    def __setitem__(self, key, value):
        debug("SETITEM:[%s]%s->%s", id(self), key, value)
        self.values[key] = EnvironmentValue(value)
        self._dict[key] = value

        self.key_set.add(key)

        # TODO: Now reevaluate any keys which depend on this value for better caching?
        self.update_cached_values(key)

    def __contains__(self, item):
        return item in self.values

    def __getitem__(self, item):
        return self.values[item]

    def Dictionary(self, *args):
        """
        Create a dictionary we can pass to eval().
        :param args:
        :return: dictionary of strings and their values.
        """
        if not args:
            return self._dict
        dlist = [self._dict[x] for x in args]
        if len(dlist) == 1:
            dlist = dlist[0]
        return dlist

    def update_cached_values(self, key):
        """
        Key has been set/changed. We need to effectively flush the cached subst values
        (If there are any).
        Also we may change the type of parsed parts of values.

        TODO: Does this need to recursively be re-evaluated?

        :param key: The changed key in the environment.
        :return: None.
        """

        # Find all variables which depend on the key

        # to_update = set([v for v in self.values if key in self.values[v].depends_on])
        to_update = set([k for k, v in self.values.items() if key in v.depends_on])

        # then recursively find all the other EnvironmentValue depending on
        # any keys which depend on the EnvironmentValue which depend on the key

        count = 0
        while to_update:

            # Create a list of all values which need to be update by our current list
            # of to_update variables. This should recursively give us a list of
            # all variables invalidated by the key being changed.

            next_to_update = set([k for k, v in self.values.items()
                                  for u in to_update
                                  #  k not in u ??? or  k != u ???
                                  if k not in u and u in v.depends_on])
            if not next_to_update:
                break

            to_update.update(next_to_update)

            debug("Pass [%6d]", count)
            count += 1

        for k in to_update:
            self.values[k].update(key, self.values)

    @staticmethod
    def split_dependencies(value, string_values, parsed_values):
        """
        Split the dependencies for the value into strings and items which need further processing
        (parsed_values
        :param string_values:
        :param parsed_values:
        :return:
        """

        # Break parts in to simple strings or parts to be further evaluated
        for (t, v, i) in value.all_dependencies:
            if t in (ValueTypes.ESCAPE_START, ValueTypes.ESCAPE_END):
                string_values[i] = (v, t)
            elif t in (ValueTypes.VARIABLE, ValueTypes.EVALUABLE, ValueTypes.FUNCTION_CALL):
                parsed_values[i] = (t, v, i)
            elif t == ValueTypes.CALLABLE:
                parsed_values[i] = (t, v, i)
            elif t == ValueTypes.VARIABLE_OR_CALLABLE:
                parsed_values[i] = (t, v, i)
            else:
                string_values[i] = (v, t)

        debug("Parsed values:%s  for %s [%s]" % (parsed_values, value._parsed, string_values))

    def resolve_unassigned_types(self, parsed_values, string_values, gvars, lvars):
        """
        Use the context to remove uncertainty on types
        :param parsed_values:
        :param gvars:
        :param lvars:
        :return:
        """

        # Now we should be able to resolve if value is a callable or a variable.
        # if unsure, we'll leave as callable.
        for pv in parsed_values:
            if pv is None:
                continue
            (t, v, i) = pv

            nt = t
            # We should be able to resolve now if it's a variable or a callable.
            if t == ValueTypes.EVALUABLE:
                if '(' in v:
                    nt = ValueTypes.EVALUABLE
                elif '.' not in v and '[' not in v:
                    nt = ValueTypes.PARSED
                    if v not in lvars and v in self:
                        # Something like ${VARIABLE}
                        nt = self[v].var_type
                    elif v in lvars:
                        nt = ValueTypes.PARSED  # Should we have lvars type?
                    else:
                        if NameError not in AllowableExceptions:
                            raise_exception(NameError(v), lvars['TARGETS'], self.value)
                        else:
                            # It's ok to have an undefined variable. Just replace with blank.
                            string_values[i] = ''
                            parsed_values[i] = None

                else:
                    # Something else where eval'ing it should suffice to yield a good value.
                    t = ValueTypes.EVALUABLE
            if nt != t:
                # We've modified the type, so update parsed values
                parsed_values[i] = (nt, v, i)

        debug("==============================")
        debug("After resolving unknown types:")
        EnvironmentValue.debug_print_parsed_parts(parsed_values)

    def evaluate_parsed_values(self, parsed_values, string_values, source, target, gvars, lvars, for_signature):
        """
        Walk the list of parsed values and evaluate each in turn.  Possible return values for each are:
        * A plain string (No $ values)
        * A non-plain string (requires parsing and evaluation)
        * A collection of values (all plain strings)
        * A collection of values (not ALL plain strings)
        * Callable object

        :param parsed_values:
        :param string_values:
        :param gvars:
        :param lvars:
        :return:
        """

        for pv in parsed_values:
            if pv is None:
                continue
            (t, v, i) = pv

            # # Below should only apply if it's a variable and it's not defined.
            # # not for callables. callables can come from CALLABLE or VARIABLE_OR_CALLABLE
            #
            # if NameError not in AllowableExceptions:
            #     raise_exception(NameError(v), lvars['TARGETS'], self.value)
            # else:
            #     # It's ok to have an undefined variable. Just replace with blank.
            #     string_values[i] = ''
            #     parsed_values[i] = None
            #     continue

            # At this point it's possible to determine if we guessed callable correctly
            # or if it's actually evaluable
            if t == ValueTypes.CALLABLE:
                if callable(v):
                    t = ValueTypes.CALLABLE
                else:
                    debug("Swapped to EVALUABLE:%s" % v)
                    t = ValueTypes.EVALUABLE

            # Now handle all the various types which can be in the value.
            if t == ValueTypes.STRING:
                # The variable resolved to a string . No need to process further.
                debug("STR      Type:%s VAL:%s" % (pv[0], pv[1]))
                string_values[i] = (env[v].value, t)
                debug("%s->%s" % (v, string_values[i]))
                parsed_values[i] = None
            elif t == ValueTypes.PARSED:
                debug("PARSED   Type:%s VAL:%s" % (pv[0], pv[1]))

                try:
                    try:
                        value = lvars[v]
                    except KeyError as e:
                        value = env[v].value

                    # Shortcut self reference
                    if isinstance(value, Number):
                        string_values[i] = (value, ValueTypes.NUMBER)
                    elif is_String(value) and len(value) > 1 and value[0] == '$' and value[1:] == v:
                        # TODO: Is this worth doing? (Check line profiling once we get all functionality working)
                        # Special case, variables value references itself and only itself
                        string_values[i] = ('', ValueTypes.STRING)
                    else:
                        # TODO: Handle other recursive loops by empty stringing this value before recursing with copy of lvar?
                        string_values[i] = (env[v].subst(env, mode, target, source, gvars, lvars, conv),
                                            ValueTypes.STRING)
                    debug("%s->%s" % (v, string_values[i]))
                except KeyError as e:
                    # Must be lvar
                    if v[0] == '{' or '.' in v:
                        value = eval(v, gvars, lvars)
                    else:
                        value = str(lvars[v])

                    string_values[i] = (value, ValueTypes.STRING)
                parsed_values[i] = None
            elif t == ValueTypes.CALLABLE:
                to_call = v

                call_value = self.eval_callable(to_call, parsed_values, string_values, target=target,
                                                source=source, gvars=gvars, lvars=lvars, for_sig=for_signature)

                # TODO: Handle return value not being a string, (a collection for example)

                if is_String(call_value) and '$' not in call_value:
                    string_values[i] = (call_value, ValueTypes.STRING)
                elif is_Sequence(call_value):
                    # TODO: Handle if need to put back in parsed_values.. (check for $ in any element)
                    # and/or call subst..
                    string_values[i] = (" ".join(call_value), ValueTypes.STRING)
                    part_string_value = []
                    for part in call_value:
                        if is_String(part) and '$' not in part:
                            part_string_value.append(part)
                        else:
                            try:
                                part_string_value.append(self[part].subst(env, mode, target, source, gvars,
                                                                          lvars, conv))
                            except KeyError as e:
                                ev = EnvironmentValue(call_value)
                                string_values[i] = (
                                    ev.subst(env, mode, target, source, gvars, lvars, conv), ValueTypes.STRING)

                else:
                    # Returned value has a $ in it.
                    try:
                        string_values[i] = env[call_value].subst(env, mode, target, source, gvars, lvars, conv)
                    except KeyError as e:
                        ev = EnvironmentValue(call_value)
                        string_values[i] = (ev.subst(env, mode, target, source, gvars, lvars, conv), ValueTypes.STRING)

                parsed_values[i] = None
            elif t == ValueTypes.NUMBER:
                # The variable resolved to a number. No need to process further.
                debug("Num      Type:%s VAL:%s" % (pv[0], pv[1]))
                string_values[i] = (str(env[v].value), t)
                debug("%s->%s" % (v, string_values[i]))
                parsed_values[i] = None
            elif t == ValueTypes.COLLECTION:
                # Handle list, tuple, or dictionary
                # Basically iterate all items, evaluating each, and then join them together with a space
                debug("COLLECTION  Type:%s VAL:%s" % (t, v))
                value = env[v].value

                # TODO: Finish implementation
                # This is very simple implementation which ignores nested collections and values which
                # need to be further evaluated.(subst'd)
                string_values[i] = (" ".join(value), t)
                parsed_values[i] = None
            elif t in (ValueTypes.EVALUABLE, ValueTypes.FUNCTION_CALL):
                try:
                    # Note: this may return a callable or a string or a number or an object.
                    # If it's callable, then it needs be executed by the logic above for ValueTypes.CALLABLE.
                    sval = eval(v, gvars, lvars)
                except AllowableExceptions as e:
                    sval = ''
                if is_String(sval):
                    string_values[i] = (sval, ValueTypes.NUMBER)
                    parsed_values[i] = None
                elif callable(sval):
                    parsed_values[i] = (ValueTypes.CALLABLE, sval, i)
            elif t == ValueTypes.VARIABLE_OR_CALLABLE and v not in lvars and v not in gvars:
                string_values[i] = ('', ValueTypes.STRING)  # not defined so blank string
                parsed_values[i] = None
            else:
                debug("AAHAHAHHAH BROKEN")
                import pdb;
                pdb.set_trace()

    @staticmethod
    def subst(item, env, mode=0, target=None, source=None, gvars={}, lvars={}, conv=None):
        """
        Recursively Expand string
        :param item: The string to be expanded.
        :param env:
        :param mode: 0 = leading or trailing white space will be removed from the result. and all sequences of white
                        space will be compressed to a single space character. Additionally, any $( and $) character
                        sequences will be stripped from the returned string
                    1 = preserve white space and $(-$) sequences.
                    2 = strip all characters between any $( and $) pairs (as is done for signature calculation)
                    Must be one of the SubstModes values.
        :param target: list of target Nodes
        :param source: list of source Nodes - Both target and source must be set if $TARGET, $TARGETS, $SOURCE and
                       $SOURCES are to be available for expansion
        :param conv: may specify a conversion function that will be used in place of the default. For example,
                     if you want Python objects (including SCons Nodes) to be returned as Python objects, you can use
                     the Python lambda idiom to pass in an unnamed function that simply returns its unconverted argument.
        :param gvars: Specify the global variables. Defaults to empty dict, which will yield using this EnvironmentValues
                      symbols.
        :param lvars: Specify local variables to evaluation the variable with. Usually this is provided by executor.
        :return: expanded string
        """

        for_signature = mode == SubstModes.FOR_SIGNATURE

        # TODO:Figure out how to handle this properly.  May involve seeing if source & targets is specified. Or checking
        #      against list of variables which are "ok" to leave undefined and unexpanded in the returned string and/or
        #      the cached values.  This is likely important for caching CCCOM where the TARGET/SOURCES will change
        #      and there is still much value in caching whatever else can be cached from such strings
        ignore_undefined = False

        use_cache_item = 0
        if for_signature:
            use_cache_item = 1

        if not gvars:
            gvars = env._dict

        if 'TARGET' not in lvars:
            d = create_subst_target_source_dict(target, source)
            if d:
                lvars = lvars.copy()
                lvars.update(d)

        # Stick the element in a list, evaluate all elements in list until empty/all evaluated.
        # If evaluation returns a list from a single element insert that new list at the point the element
        # being evaluated was previously at.

        # First retrieve the value (If it exists)

        try:
            val = env.values[item]
        except KeyError as e:
            # No such value create one
            val = EnvironmentValue(item)

        string_values = [None] * len(val.all_dependencies)
        parsed_values = [None] * len(val.all_dependencies)

        env.split_dependencies(val, string_values, parsed_values)

        while any(parsed_values):
            # Now we can use the context (env, gvars, lvars) to decide
            # any uncertain types in parsed_values
            env.resolve_unassigned_types(parsed_values, string_values, gvars, lvars)

            # Now evaluate the parsed values. Not that some of these may expand into
            # multiple values and require expansion of parsed_values and string_values array
            env.evaluate_parsed_values(parsed_values, string_values, source, target, gvars, lvars, for_signature)

        try:
            var_type = env.values[item].var_type

            if var_type == ValueTypes.STRING:
                return env.values[item].value
            elif var_type == ValueTypes.PARSED:
                return env.values[item].subst(env, mode=mode, target=target, source=source, gvars=gvars,
                                              lvars=lvars, conv=conv)
            elif var_type == ValueTypes.CALLABLE:
                # From Subst.py
                # try:
                #     s = s(target=lvars['TARGETS'],
                #           source=lvars['SOURCES'],
                #           env=self.env,
                #           for_signature=(self.mode != SUBST_CMD))
                # except TypeError:
                #     # This probably indicates that it's a callable
                #     # object that doesn't match our calling arguments
                #     # (like an Action).
                #     if self.mode == SUBST_RAW:
                #         return s
                #     s = self.conv(s)
                # return self.substitute(s, lvars)
                # TODO: This can return a non-plain-string result which needs to be further processed.
                retval = env.values[item].value(target, source, env, (mode == SubstModes.FOR_SIGNATURE))
                return retval
        except KeyError as e:
            if is_String(item):
                # The value requested to be substituted doesn't exist in the EnvironmentVariables.
                # So, let's create a new value?
                # Currently we're naming it the same as it's content.
                # TODO: Should we keep these separate from the variables? We're caching both..
                env.values[item] = EnvironmentValue(item)
                return env.values[item].subst(env, mode=mode, target=target, source=source, gvars=gvars,
                                              lvars=lvars, conv=conv)
            elif is_Sequence(item):
                return [EnvironmentValue(v).subst(env, mode=mode, target=target, source=source, gvars=gvars,
                                                  lvars=lvars, conv=conv) for v in item]

    @staticmethod
    def subst_list(listSubstVal, env, mode=SubstModes.RAW,
                   target=None, source=None, gvars={}, lvars={}, conv=None):
        """Substitute construction variables in a string (or list or other
        object) and separate the arguments into a command list.

        The companion subst() function (above) handles basic substitutions within strings, so see
        that function instead if that's what you're looking for.

        :param listSubstVal: Either a string (potentially with embedded newlines),
                     or a list of command line arguments
        :param env:
        :param mode:
        :param target:
        :param source:
        :param gvars:
        :param lvars:
        :param conv:
        :return:  a list of lists of substituted values (First dimension is separate command line,
                  second dimension is "words" in the command line)

        """
        for_signature = mode == SubstModes.FOR_SIGNATURE

        # TODO: Fill in gvars, lvars as env.Subst() does..
        if 'TARGET' not in lvars:
            d = create_subst_target_source_dict(target, source)
            if d:
                lvars = lvars.copy()
                lvars.update(d)

        retval = [[]]
        retval_index = 0

        if is_String(listSubstVal) and not isinstance(listSubstVal, CmdStringHolder):
            # listSubstVal = str(listSubstVal)  # In case it's a UserString.
            # listSubstVal = separate_args.findall(listSubstVal)
            listSubstVal = separate_args.findall(str(listSubstVal))  # In case it's a UserString.

            # Drop white space from splits. We'll be (likely) joining the elements with white space
            # and/or providing directly as arguments to popen().
            # listSubstVal = [i for i in listSubstVal if i[0] not in ' \t\r\f\v']
            listSubstVal = [i for i in listSubstVal if not i[0].isspace()]

            try:
                debug("subst_list:IsString:%s", listSubstVal)
            except TypeError as e:
                debug("LKDJFKLSDJF")
        else:
            debug("subst_list:NotString:%r", listSubstVal)

        for element in listSubstVal:
            # TODO: Implement splitting into multiple commands if there's a NEWLINE in any element.

            # for a in args:
            #     if a[0] in ' \t\n\r\f\v':
            #         if '\n' in a:
            #             self.next_line()
            #         elif within_list:
            #             self.append(a)
            #         else:
            #             self.next_word()
            #     else:
            #         self.expand(a, lvars, within_list)
            if '\n' in element:
                retval_index += 1
                retval.append([])

            e_value = EnvironmentValue(element, element)
            this_value = e_value.subst(env, mode=mode,
                                       target=target, source=source,
                                       gvars=gvars, lvars=lvars, conv=conv)

            # Now we need to determine if we need to recurse/evaluate this_value
            if '$' not in this_value:
                # no more evaluation needed
                args = [a for a in separate_args.findall(this_value) if not a.isspace()]

                retval[retval_index].extend(args)
            else:
                # Need to handle multiple levels of recursion, also it's possible
                # that escaping could span several levels of recursion so $( at top , and $)
                # several levels lower. (This would be unwise.. do we need to enable this)
                this_value_2 = EnvironmentValues.subst_list(this_value, env, mode, target, source, gvars, lvars, conv)

                debug("need to recurse")
                raise Exception("need to recurse in subst_list: [%s]->{%s}" % (this_value, this_value_2))

        debug("subst_list:%s", retval[retval_index])

        return retval
