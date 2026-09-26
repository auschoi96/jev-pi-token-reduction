"""Thirty public-source behavioral tasks, with executable independent oracles.

Each task requires reasoning through multiple interacting branches of one real
CPython module. The agent sees source and case code, never the expected answers.
These assess source comprehension, not code-editing success.
"""
from pathlib import Path
import importlib.util
import json
import sys
import sysconfig


def case(name, code):
    return {"name": name, "code": code}


TASKS = []


def task(module, name, symbols, cases):
    TASKS.append({"id": f"{module.replace('/', '_')}_{name}", "module": module,
                  "symbols": symbols, "cases": cases})


task("textwrap", "whitespace", ["TextWrapper._munge_whitespace", "TextWrapper._wrap_chunks"], [
    case("tabs", r"return m.wrap('a\tb  c\nd', width=6, tabsize=4)"),
    case("preserved", r"return m.wrap('  alpha   beta  gamma ', width=10, drop_whitespace=False)"),
    case("no_replace", r"return m.wrap('alpha\nbeta\tgamma', width=9, replace_whitespace=False, expand_tabs=False)"),
])
task("textwrap", "hyphens", ["TextWrapper._split", "TextWrapper._handle_long_word"], [
    case("default", r"return m.wrap('alpha-beta-gamma delta', width=8)"),
    case("no_hyphens", r"return m.wrap('alpha-beta-gamma delta', width=8, break_on_hyphens=False)"),
    case("no_long_words", r"return m.wrap('alpha-beta-gamma delta', width=8, break_long_words=False, break_on_hyphens=False)"),
    case("punctuation", r"return m.wrap('Wait--really? one-two-three', width=10)"),
])
task("textwrap", "limits", ["TextWrapper._wrap_chunks", "shorten"], [
    case("max_lines", r"return m.wrap('one two three four five six seven', width=11, max_lines=2, placeholder=' ...')"),
    case("shorten", r"return m.shorten('  one\t two\nthree four ', width=13, placeholder='...')"),
    case("oversized_placeholder", r"return m.wrap('hello world', width=3, max_lines=1, placeholder='[more]')"),
    case("indent_budget", r"return m.wrap('one two three four five', width=10, initial_indent='> ', subsequent_indent='..', max_lines=2, placeholder='!')"),
])
task("textwrap", "indentation", ["dedent", "indent"], [
    case("mixed_tabs", r"return m.dedent('  alpha\n\tbeta\n  gamma\n')"),
    case("blank_lines", r"return m.dedent('    one\n   \n      two\n    three\n')"),
    case("default_indent", r"return m.indent('one\n\n  \ntwo\n', '> ')"),
    case("all_lines", r"return m.indent('one\n\n  \ntwo\n', '> ', predicate=lambda line: True)"),
])
task("textwrap", "sentences", ["TextWrapper._fix_sentence_endings", "TextWrapper._wrap_chunks"], [
    case("fixed", r"return m.wrap('Hello world. Next sentence! Last one?', width=24, fix_sentence_endings=True)"),
    case("quotes", r"return m.fill('He said \"yes.\" Then left. A final line.', width=22, fix_sentence_endings=True)"),
    case("indent_long_word", r"return m.wrap('abcdefghijk next', width=7, initial_indent='>>', subsequent_indent='.')"),
])

task("graphlib", "ready_order", ["TopologicalSorter.prepare", "TopologicalSorter.get_ready", "TopologicalSorter.done"], [
    case("layers", "s=m.TopologicalSorter({'build':{'compile','lint'}, 'compile':{'fetch'}, 'lint':{'fetch'}})\ns.prepare()\nout=[]\nwhile s.is_active():\n    ready=s.get_ready(); out.append(sorted(ready)); s.done(*ready)\nreturn out"),
    case("repeat_get_ready", "s=m.TopologicalSorter({'b':{'a'}}); s.prepare()\nfirst=s.get_ready(); second=s.get_ready(); s.done(*first)\nreturn [list(first), list(second), list(s.get_ready()), s.is_active()]"),
    case("duplicate_dependency", "s=m.TopologicalSorter(); s.add('b','a','a'); s.add('b','a'); s.prepare()\nfirst=s.get_ready(); s.done(*first)\nreturn [list(first), list(s.get_ready())]"),
])
task("graphlib", "lifecycle_errors", ["TopologicalSorter.add", "TopologicalSorter.prepare", "TopologicalSorter.get_ready"], [
    case("not_prepared", "return m.TopologicalSorter({'a':set()}).get_ready()"),
    case("prepare_twice", "s=m.TopologicalSorter(); s.prepare(); return s.prepare()"),
    case("add_after_prepare", "s=m.TopologicalSorter(); s.prepare(); return s.add('x')"),
    case("empty", "s=m.TopologicalSorter(); s.prepare(); return [s.is_active(), list(s.get_ready()), bool(s)]"),
])
task("graphlib", "done_validation", ["TopologicalSorter.done", "TopologicalSorter.get_ready"], [
    case("unknown", "s=m.TopologicalSorter({'b':{'a'}}); s.prepare(); return s.done('z')"),
    case("not_ready", "s=m.TopologicalSorter({'b':{'a'}}); s.prepare(); return s.done('b')"),
    case("not_returned", "s=m.TopologicalSorter({'a':set()}); s.prepare(); return s.done('a')"),
    case("done_twice", "s=m.TopologicalSorter({'a':set()}); s.prepare(); s.get_ready(); s.done('a'); return s.done('a')"),
])
task("graphlib", "cycles", ["TopologicalSorter.prepare", "TopologicalSorter._find_cycle", "TopologicalSorter.get_ready"], [
    case("self_cycle", "s=m.TopologicalSorter({'a':{'a'}})\ntry: s.prepare()\nexcept m.CycleError as e: return [type(e).__name__, list(e.args[1]), list(s.get_ready())]"),
    case("partial_progress", "s=m.TopologicalSorter({'a':{'b'},'b':{'a'},'c':set(),'d':{'c'}})\ntry: s.prepare()\nexcept m.CycleError: pass\nfirst=s.get_ready(); s.done(*first); second=s.get_ready(); s.done(*second)\nreturn [list(first),list(second),s.is_active()]"),
    case("simple_cycle", "s=m.TopologicalSorter({'a':['b'],'b':['c'],'c':['a']})\ntry: s.prepare()\nexcept m.CycleError as e: return list(e.args[1])"),
])
task("graphlib", "incremental_graph", ["TopologicalSorter.add", "TopologicalSorter.static_order", "TopologicalSorter.done"], [
    case("union", "s=m.TopologicalSorter(); s.add('c','a'); s.add('c','b'); s.add('d','c'); return list(s.static_order())"),
    case("partial_done", "s=m.TopologicalSorter({'c':['a','b'],'d':['a']}); s.prepare(); s.get_ready(); s.done('a'); one=s.get_ready(); s.done('b'); two=s.get_ready(); return [list(one),list(two),s.is_active()]"),
    case("insertion", "s=m.TopologicalSorter(); s.add('z'); s.add('y'); s.add('x','z','y'); return list(s.static_order())"),
])

task("configparser", "precedence", ["RawConfigParser._unify_values", "RawConfigParser.get", "SectionProxy.__getitem__"], [
    case("vars_section_defaults", r"c=m.ConfigParser(defaults={'color':'blue','size':'large'}); c.read_string('[x]\ncolor=red\n'); return [c.get('x','color',vars={'COLOR':'green'}),c.get('x','size'),dict(c['x'])]"),
    case("fallback", r"c=m.ConfigParser(defaults={'a':'default'}); c.add_section('x'); return [c.get('x','a',fallback='fallback'),c.get('x','b',fallback='fallback'),c.get('missing','a',fallback='fallback')]"),
    case("remove_default", r"c=m.ConfigParser(defaults={'a':'default'}); c.read_string('[x]\na=local\n'); c.remove_option('x','a'); return [c.get('x','a'),c.has_option('x','a'),c.options('x')]"),
])
task("configparser", "interpolation", ["BasicInterpolation._interpolate_some", "RawConfigParser.get"], [
    case("recursive", r"c=m.ConfigParser(); c.read_string('[DEFAULT]\nroot=/srv\n[x]\nbase=%(root)s/app\npath=%(base)s/data\n'); return [c.get('x','path'),c.get('x','path',raw=True),c.get('x','path',vars={'root':'/tmp'})]"),
    case("escaped", r"c=m.ConfigParser(); c.read_string('[x]\nrate=90%%\n'); return c.get('x','rate')"),
    case("missing_ref", r"c=m.ConfigParser(); c.read_string('[x]\na=%(missing)s\n'); return c.get('x','a',fallback='fallback')"),
])
task("configparser", "extended_interpolation", ["ExtendedInterpolation._interpolate_some", "RawConfigParser.get"], [
    case("cross_section", r"c=m.ConfigParser(interpolation=m.ExtendedInterpolation()); c.read_string('[paths]\nroot=/srv\n[app]\nname=web\nfile=${paths:root}/${name}/$$cache\n'); return [c.get('app','file'),c.get('app','file',raw=True)]"),
    case("missing_section", r"c=m.ConfigParser(interpolation=m.ExtendedInterpolation()); c.read_string('[x]\na=${missing:key}\n'); return c.get('x','a')"),
    case("invalid_reference", r"c=m.ConfigParser(interpolation=m.ExtendedInterpolation()); c.read_string('[x]\na=${a:b:c}\n'); return c.get('x','a')"),
])
task("configparser", "parsing", ["RawConfigParser._read", "RawConfigParser.read_string"], [
    case("duplicates", r"c=m.ConfigParser(strict=False); c.read_string('[x]\na=first\na=second\n[x]\nb=third\n'); return dict(c['x'])"),
    case("strict_duplicate", r"c=m.ConfigParser(); c.read_string('[x]\na=first\na=second\n'); return dict(c['x'])"),
    case("comments_continuation", r"c=m.ConfigParser(inline_comment_prefixes=('#',)); c.read_string('[x]\na=one#two\nb=one # two\nc=first\n    second\n'); return dict(c['x'])"),
])
task("configparser", "converters", ["RawConfigParser._get_conv", "RawConfigParser._convert_to_boolean", "SectionProxy.get"], [
    case("booleans", r"c=m.ConfigParser(); c.read_dict({'x':{'a':'YES','b':'off','c':'1','d':'false'}}); return [c.getboolean('x',k) for k in ('a','b','c','d')]"),
    case("bad_bool", r"c=m.ConfigParser(); c.read_dict({'x':{'a':'perhaps'}}); return c.getboolean('x','a',fallback=False)"),
    case("typed_fallback", r"c=m.ConfigParser(); c.add_section('x'); return [c.getint('x','a',fallback='7'),c['x'].get('a'),c.getfloat('missing','a',fallback=1.5)]"),
])

task("difflib", "ratios", ["SequenceMatcher.ratio", "SequenceMatcher.quick_ratio", "SequenceMatcher.real_quick_ratio"], [
    case("reordered", r"s=m.SequenceMatcher(None,'abcd','bcde'); return [s.ratio(),s.quick_ratio(),s.real_quick_ratio()]"),
    case("same_multiset", r"s=m.SequenceMatcher(None,'abca','caba'); return [s.ratio(),s.quick_ratio(),s.real_quick_ratio()]"),
    case("empty", r"s=m.SequenceMatcher(None,'',''); return [s.ratio(),s.quick_ratio(),s.real_quick_ratio()]"),
])
task("difflib", "opcodes", ["SequenceMatcher.get_matching_blocks", "SequenceMatcher.get_opcodes"], [
    case("mixed_edits", r"s=m.SequenceMatcher(None,'qabxcd','abycdf'); return [s.get_matching_blocks(),s.get_opcodes()]"),
    case("pure_insert", r"return m.SequenceMatcher(None,'','abc').get_opcodes()"),
    case("pure_delete", r"return m.SequenceMatcher(None,'abc','').get_opcodes()"),
])
task("difflib", "junk", ["SequenceMatcher.find_longest_match", "SequenceMatcher.__chain_b"], [
    case("spaces", r"s=m.SequenceMatcher(lambda x:x==' ',' abcd','abcd abcd'); return list(s.find_longest_match(0,5,0,9))"),
    case("autojunk", r"a='b'+'a'*210; b='c'+'a'*210; return [m.SequenceMatcher(None,a,b).ratio(),m.SequenceMatcher(None,a,b,autojunk=False).ratio()]"),
    case("tie", r"s=m.SequenceMatcher(None,'abXXcd','cdYYab'); return list(s.find_longest_match())"),
])
task("difflib", "delta_formats", ["unified_diff", "ndiff", "restore"], [
    case("unified", r"return list(m.unified_diff(['a\n','b\n','c\n'],['a\n','B\n','c\n'],fromfile='old',tofile='new',n=0))"),
    case("restore", r"a=['one\n','two\n']; b=['one\n','three\n']; delta=list(m.ndiff(a,b)); return [list(m.restore(delta,1)),list(m.restore(delta,2))]"),
    case("bad_restore", r"return list(m.restore(['  a\n'],3))"),
])
task("difflib", "grouping", ["SequenceMatcher.get_grouped_opcodes", "get_close_matches"], [
    case("context_zero", r"s=m.SequenceMatcher(None,'abcde','aXcdY'); return list(s.get_grouped_opcodes(0))"),
    case("no_changes", r"return list(m.SequenceMatcher(None,'abc','abc').get_grouped_opcodes(1))"),
    case("close", r"return m.get_close_matches('appel',['ape','apple','peach','puppy'],n=2,cutoff=0.5)"),
    case("invalid_count", r"return m.get_close_matches('x',['x'],n=0)"),
])

task("urllib/parse", "url_components", ["urlsplit", "urlparse", "_splitnetloc"], [
    case("credentials_ipv6", r"r=m.urlsplit('https://user:pw@[::1]:8443/a;b?q=1#frag'); return [list(r),r.hostname,r.port,r.username,r.password]"),
    case("params", r"return [list(m.urlparse('http://h/a;b/c;d?x#f')),list(m.urlsplit('http://h/a;b/c;d?x#f'))]"),
    case("no_fragments", r"return list(m.urlsplit('http://h/a?x#frag',allow_fragments=False))"),
])
task("urllib/parse", "joining", ["urljoin", "urlparse"], [
    case("relative", r"base='http://a/b/c/d;p?q'; return [m.urljoin(base,x) for x in ('g','../g','../../g','?y','#s','//other/x')]"),
    case("dot_segments", r"return m.urljoin('https://h/a/b/c','.././../d/./e/../f')"),
    case("absolute_scheme", r"return [m.urljoin('https://h/a','mailto:user@example.com'),m.urljoin('https://h/a','/x/../y')]"),
])
task("urllib/parse", "query_parsing", ["parse_qsl", "parse_qs"], [
    case("blanks_repeated", r"q='a=1&a=2&b=&c&d=hello+world'; return [m.parse_qs(q),m.parse_qsl(q,keep_blank_values=True)]"),
    case("strict", r"return m.parse_qsl('a=1&broken&b=2',strict_parsing=True)"),
    case("limit", r"return m.parse_qsl('a=1&b=2&c=3',max_num_fields=2)"),
    case("separator", r"return [m.parse_qsl('a=1;b=2&c=3'),m.parse_qsl('a=1;b=2&c=3',separator=';')]"),
])
task("urllib/parse", "encoding", ["quote", "quote_plus", "unquote", "urlencode"], [
    case("quote_modes", r"return [m.quote('a/b c+é'),m.quote_plus('a/b c+é'),m.quote('a/b',safe='')]"),
    case("decode_modes", r"return [m.unquote('a+b%2Fc'),m.unquote_plus('a+b%2Fc'),m.unquote('%FF')]"),
    case("sequences", r"q=[('a',[1,2]),('b',[]),('c','x y')]; return [m.urlencode(q),m.urlencode(q,doseq=True)]"),
])
task("urllib/parse", "validation", ["_NetlocResultMixinBase.port", "_checknetloc", "urlsplit"], [
    case("port_range", r"return m.urlsplit('http://example.com:70000/').port"),
    case("bad_port", r"return m.urlsplit('http://example.com:abc/').port"),
    case("strip_controls", r"return list(m.urlsplit(' \x00https://exa\nmple.com/a\tb\rc'))"),
    case("bytes", r"r=m.urlsplit(b'https://host/a?q=1'); return list(r.decode())"),
])

task("argparse", "defaults", ["ArgumentParser.parse_known_args", "ArgumentParser._parse_known_args"], [
    case("string_default", r"p=m.ArgumentParser(); p.add_argument('--n',type=int,default='5'); a=p.parse_args([]); return [vars(a),type(a.n).__name__]"),
    case("existing_namespace", r"p=m.ArgumentParser(); p.add_argument('--n',type=int,default='5'); a=p.parse_args([],namespace=m.Namespace(n='7')); return [vars(a),type(a.n).__name__]"),
    case("suppress", r"p=m.ArgumentParser(argument_default=m.SUPPRESS); p.add_argument('--x'); p.add_argument('--y',default='yes'); return vars(p.parse_args([]))"),
])
task("argparse", "nargs", ["ArgumentParser._get_values", "ArgumentParser._match_argument"], [
    case("optional_value", r"p=m.ArgumentParser(); p.add_argument('--level',nargs='?',const='AUTO',default='OFF'); return [vars(p.parse_args(x)) for x in ([],['--level'],['--level','high'])]"),
    case("star_positional", r"p=m.ArgumentParser(); p.add_argument('items',nargs='*',default=['fallback']); return [vars(p.parse_args([])),vars(p.parse_args(['a','b']))]"),
    case("remainder", r"p=m.ArgumentParser(); p.add_argument('--x'); p.add_argument('rest',nargs=m.REMAINDER); return vars(p.parse_args(['--x','a','cmd','--unknown','b']))"),
])
task("argparse", "actions", ["_AppendAction.__call__", "_ExtendAction.__call__", "_CountAction.__call__"], [
    case("append_default", r"p=m.ArgumentParser(); p.add_argument('--x',action='append',default=['base']); return vars(p.parse_args(['--x','one','--x','two']))"),
    case("extend", r"p=m.ArgumentParser(); p.add_argument('--x',action='extend',nargs='+'); return vars(p.parse_args(['--x','a','b','--x','c']))"),
    case("count", r"p=m.ArgumentParser(); p.add_argument('-v',action='count'); return [vars(p.parse_args([])),vars(p.parse_args(['-vvv']))]"),
    case("boolean", r"p=m.ArgumentParser(); p.add_argument('--feature',action=m.BooleanOptionalAction); return [vars(p.parse_args(x)) for x in ([],['--feature'],['--no-feature'])]"),
])
task("argparse", "known_options", ["ArgumentParser._parse_optional", "ArgumentParser._get_option_tuples", "ArgumentParser.parse_known_args"], [
    case("abbreviation", r"p=m.ArgumentParser(); p.add_argument('--foobar'); a,rest=p.parse_known_args(['--foo','x','--other','y']); return [vars(a),rest]"),
    case("no_abbreviation", r"p=m.ArgumentParser(allow_abbrev=False); p.add_argument('--foobar'); a,rest=p.parse_known_args(['--foo','x']); return [vars(a),rest]"),
    case("negative_number", r"p=m.ArgumentParser(); p.add_argument('n',type=float); return vars(p.parse_args(['-2.5']))"),
    case("double_dash", r"p=m.ArgumentParser(); p.add_argument('--flag',action='store_true'); p.add_argument('rest',nargs='*'); return vars(p.parse_args(['--','--flag','-3']))"),
])
task("argparse", "composition", ["_SubParsersAction.__call__", "_ActionsContainer.set_defaults", "_ActionsContainer._handle_conflict_resolve"], [
    case("subparser_defaults", r"p=m.ArgumentParser(); p.set_defaults(kind='root'); sub=p.add_subparsers(dest='cmd'); child=sub.add_parser('run'); child.set_defaults(kind='child'); child.add_argument('--n',type=int,default=2); return [vars(p.parse_args([])),vars(p.parse_args(['run','--n','3']))]"),
    case("conflict_resolve", r"p=m.ArgumentParser(conflict_handler='resolve'); p.add_argument('-f','--foo',dest='old',default='OLD'); p.add_argument('--foo',dest='new',default='NEW'); return vars(p.parse_args(['-f','a','--foo','b']))"),
    case("parent", r"parent=m.ArgumentParser(add_help=False); parent.add_argument('--shared',default='base'); p=m.ArgumentParser(parents=[parent]); p.set_defaults(shared='child'); return vars(p.parse_args([]))"),
])


def load_source(module):
    path = Path(sysconfig.get_path("stdlib")) / (module + ".py")
    spec = importlib.util.spec_from_file_location("jev_benchmark_module", path)
    loaded = importlib.util.module_from_spec(spec)
    # Modules such as calendar register enums through sys.modules while executing.
    sys.modules[spec.name] = loaded
    try:
        spec.loader.exec_module(loaded)
    finally:
        sys.modules.pop(spec.name, None)
    return path, loaded


def oracle(module, code):
    scope = {}
    exec("def run(m):\n" + "\n".join("    " + line for line in code.splitlines()), scope)
    try:
        result = {"value": scope["run"](module)}
    except Exception as error:
        # Types carry stable, checkable behavior without path-dependent error messages.
        result = {"error": type(error).__name__}
    return json.loads(json.dumps(result))


def build_manifest():
    assert len(TASKS) == 30
    tasks = []
    for item in TASKS:
        path, module = load_source(item["module"])
        tasks.append({**item, "source_path": str(path), "gold": {c["name"]: oracle(module, c["code"]) for c in item["cases"]}})
    return tasks


if __name__ == "__main__":
    print(json.dumps(build_manifest(), indent=2))
