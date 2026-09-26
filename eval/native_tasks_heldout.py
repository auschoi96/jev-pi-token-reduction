"""Thirty held-out behavioral tasks over six CPython modules not in native_tasks.py (the set that calibrated
omit_risk). Same format and oracle as native_tasks.py; the agent never sees the expected answers."""
import json

from native_tasks import case, load_source, oracle

TASKS = []


def task(module, name, symbols, cases):
    TASKS.append({"id": f"{module.replace('/', '_')}_{name}", "module": module, "symbols": symbols, "cases": cases})


task("shlex", "posix_split", ["shlex.read_token", "split"], [
    case("quotes_escapes", r"""return m.split('a "b c" d\\ e \'f"g\'')"""),
    case("comments", r"return m.split('x=1 # trailing comment', comments=True)"),
    case("backslashes", r"""return m.split('a\\\\b "c\\\\d" \'e\\\\f\'')"""),
])
task("shlex", "non_posix", ["shlex.__init__", "shlex.read_token"], [
    case("keeps_quotes", r"""s=m.shlex('a "b c" d\'e f\'', posix=False); return list(s)"""),
    case("punctuation", r"s=m.shlex('one;two&&three|four', posix=True, punctuation_chars=True); return list(s)"),
    case("custom_punctuation", r"s=m.shlex('a|b c', punctuation_chars='|'); s.whitespace_split=True; return list(s)"),
])
task("shlex", "quote_join", ["quote", "join"], [
    case("quote_cases", r"""return [m.quote(''), m.quote('safe-path/x.txt'), m.quote("it's"), m.quote('a b')]"""),
    case("join", r"""return m.join(['echo', 'a b', "c'd", ''])"""),
    case("round_trip", r"""return m.split(m.join(['x y', '$HOME', "q'"]))"""),
])
task("shlex", "token_stream", ["shlex.get_token", "shlex.push_token", "shlex.read_token"], [
    case("pushback", r"s=m.shlex('a b c'); s.push_token('z'); return [s.get_token(), s.get_token(), s.get_token()]"),
    case("unterminated", r"""s=m.shlex('"unterminated'); return s.get_token()"""),
    case("posix_eof", r"s=m.shlex('a  b', posix=True); s.whitespace_split=True; return [s.get_token(), s.get_token(), s.get_token()]"),
])
task("shlex", "wordchars_comments", ["shlex.__init__", "shlex.read_token"], [
    case("default_wordchars", r"s=m.shlex('abc-def ghi.jkl # rest'); return list(s)"),
    case("extended_wordchars", r"s=m.shlex('abc-def ghi', posix=True); s.wordchars += '-'; return list(s)"),
    case("no_commenters", r"s=m.shlex('a#b c', posix=True); s.commenters=''; return list(s)"),
])

task("fractions", "construction", ["Fraction.__new__"], [
    case("strings", r"return [str(m.Fraction(' -3/6 ')), str(m.Fraction('1.25e-1')), str(m.Fraction('1_000/3')), str(m.Fraction(0.1))]"),
    case("zero_denominator", r"return str(m.Fraction('1/0'))"),
    case("rational_args", r"return [str(m.Fraction(m.Fraction(1,2), m.Fraction(1,3))), str(m.Fraction(-4, -6)), str(m.Fraction('-.5'))]"),
])
task("fractions", "limit_denominator", ["Fraction.limit_denominator"], [
    case("pi", r"f=m.Fraction('3.141592653589793'); return [str(f.limit_denominator(10)), str(f.limit_denominator(100)), str(f.limit_denominator(1000))]"),
    case("small_bounds", r"return [str(m.Fraction(1,3).limit_denominator(1)), str(m.Fraction(2,3).limit_denominator(1)), str(m.Fraction(-7,10).limit_denominator(3))]"),
    case("invalid", r"return str(m.Fraction(5,1).limit_denominator(0))"),
])
task("fractions", "rounding", ["Fraction.__round__", "Fraction.__floor__", "Fraction.__ceil__", "Fraction.__trunc__"], [
    case("half_even", r"return [round(m.Fraction(5,2)), round(m.Fraction(7,2)), round(m.Fraction(-5,2))]"),
    case("ndigits", r"return [str(round(m.Fraction(1234,1000),2)), str(round(m.Fraction(1255,1000),2)), str(round(m.Fraction(-1255,1000),-1))]"),
    case("floor_ceil", "import math\nf=m.Fraction(-7,3)\nreturn [math.floor(f), math.ceil(f), math.trunc(f), int(f)]"),
])
task("fractions", "formatting", ["Fraction.__format__"], [
    case("float_styles", r"f=m.Fraction(22,7); return [format(f,'.3f'), format(f,'>10.2f'), format(f,'e'), format(f,'.0%')]"),
    case("defaults_signs", r"return [format(m.Fraction(1,3),''), format(m.Fraction(-1,8),'+.4g'), format(m.Fraction(5,1),'_^9.1f')]"),
    case("bad_spec", r"return format(m.Fraction(1,2),'d')"),
])
task("fractions", "arithmetic_compare", ["Fraction._operator_fallbacks", "Fraction._richcmp", "Fraction.__eq__", "Fraction.__pow__"], [
    case("arithmetic", r"F=m.Fraction; return [str(F(1,3)+F(1,6)), str(F(1,3)*3), repr(F(1,2)+0.25), str(F(3,4)//F(1,3)), str(F(3,4)%F(1,3))]"),
    case("comparisons", r"F=m.Fraction; return [F(1,2)==0.5, F(1,3)==1/3, F(1,2)<0.6, F(0)==False]"),
    case("powers", r"F=m.Fraction; return [str(F(2,3)**2), str(F(4,9)**-1), repr(F(4,9)**F(1,2)), str(F(-8,27)**F(1,1))]"),
])

task("statistics", "medians", ["median", "median_low", "median_high", "median_grouped"], [
    case("even", r"d=[1,3,5,7]; return [m.median(d), m.median_low(d), m.median_high(d)]"),
    case("grouped", r"return [m.median_grouped([1,2,2,3,4,4,4,4,4,5]), m.median_grouped([52,52,53,54],interval=2)]"),
    case("empty", r"return m.median([])"),
])
task("statistics", "modes", ["mode", "multimode"], [
    case("ties", r"return [m.mode([1,1,2,2]), m.multimode('aabbbbccddddeeffffgg'), m.mode(['red','blue','blue'])]"),
    case("empty_multimode", r"return m.multimode([])"),
    case("empty_mode", r"return m.mode([])"),
])
task("statistics", "quantiles", ["quantiles"], [
    case("methods", r"d=[105,129,87,86,111,111,89,81,108,92,110,100,75,105,103,109,76,119,99,91,103,129,106,101,84,111,74,87,86,103]; return [m.quantiles(d,n=4), m.quantiles(d,n=4,method='inclusive')]"),
    case("deciles_small", r"return m.quantiles([1,2,3,4,5],n=10)"),
    case("invalid_n", r"return m.quantiles([1,2,3],n=0)"),
])
task("statistics", "means", ["mean", "fmean", "geometric_mean", "harmonic_mean", "_sum"], [
    case("exact_and_weighted", "from fractions import Fraction as F\nreturn [str(m.mean([F(1,2),F(1,3)])), m.fmean([1,2,3],weights=[3,2,1]), m.mean([1,2,3,4])]"),
    case("other_means", r"return [round(m.geometric_mean([54,24,36]),9), m.harmonic_mean([40,60]), m.harmonic_mean([40,60],weights=[5,30])]"),
    case("zero_and_negative", r"return [m.harmonic_mean([1,0,3]), m.fmean([])]"),
])
task("statistics", "spread", ["variance", "pvariance", "stdev", "_ss"], [
    case("sample_population", r"d=[2.75,1.75,1.25,0.25,0.5,1.25,3.5]; return [m.variance(d), m.pvariance(d), round(m.stdev(d),12)]"),
    case("given_center", r"return [m.variance([1,2,3,4],xbar=2), m.pvariance([1,2,3,4],mu=2)]"),
    case("too_few", r"return m.variance([5])"),
])

task("ipaddress", "parsing", ["ip_address", "ip_network", "IPv4Network.__init__"], [
    case("forms", r"return [str(m.ip_address(3232235777)), str(m.ip_address('::ffff:192.168.0.1')), str(m.ip_network('10.0.0.0/255.0.0.0'))]"),
    case("host_bits", r"return str(m.ip_network('10.0.0.1/8'))"),
    case("non_strict_and_zeros", r"return [str(m.ip_network('10.0.0.1/8', strict=False)), str(m.ip_network('192.0.2.1/0.0.0.255', strict=False))]"),
])
task("ipaddress", "subnets", ["_BaseNetwork.subnets", "_BaseNetwork.supernet"], [
    case("split", r"return [str(x) for x in m.ip_network('192.0.2.0/24').subnets(prefixlen_diff=2)]"),
    case("supernets", r"return [str(m.ip_network('192.0.2.0/24').supernet(new_prefix=20)), str(m.ip_network('192.0.2.64/26').supernet())]"),
    case("bad_prefix", r"return [str(x) for x in m.ip_network('192.0.2.0/24').subnets(new_prefix=23)]"),
])
task("ipaddress", "collapse_exclude", ["collapse_addresses", "summarize_address_range", "_BaseNetwork.address_exclude"], [
    case("collapse", r"return [str(x) for x in m.collapse_addresses([m.ip_network('192.0.2.0/25'), m.ip_network('192.0.2.128/25'), m.ip_network('192.0.3.0/24')])]"),
    case("summarize", r"return [str(x) for x in m.summarize_address_range(m.ip_address('192.0.2.5'), m.ip_address('192.0.2.20'))]"),
    case("exclude", r"return [str(x) for x in m.ip_network('10.0.0.0/24').address_exclude(m.ip_network('10.0.0.64/26'))]"),
])
task("ipaddress", "ipv6_text", ["_BaseV6._compress_hextets", "IPv6Address.ipv4_mapped", "IPv6Address.sixtofour"], [
    case("compression", r"return [str(m.ip_address('2001:0db8:0000:0000:0000:ff00:0042:8329')), m.ip_address('2001:db8::1').exploded, str(m.ip_address('0:0:0:0:0:0:0:1'))]"),
    case("embedded_v4", r"return [str(m.ip_address('::ffff:10.1.2.3').ipv4_mapped), str(m.ip_address('2002:c000:204::1').sixtofour)]"),
    case("scope", r"a=m.ip_address('fe80::1%eth0'); return [str(a), a.scope_id, str(m.ip_address('1:0:0:2:0:0:0:3'))]"),
])
task("ipaddress", "hosts_relations", ["_BaseNetwork.hosts", "_BaseNetwork.overlaps", "_BaseNetwork.subnet_of", "IPv4Network.__init__"], [
    case("hosts", r"return [[str(h) for h in m.ip_network('192.0.2.0/30').hosts()], [str(h) for h in m.ip_network('192.0.2.0/31').hosts()], [str(h) for h in m.ip_network('192.0.2.7/32').hosts()]]"),
    case("relations", r"return [m.ip_network('10.0.0.0/8').overlaps(m.ip_network('10.1.0.0/16')), m.ip_network('10.1.0.0/16').subnet_of(m.ip_network('10.0.0.0/8')), m.ip_network('10.0.0.0/8').supernet_of(m.ip_network('11.0.0.0/8'))]"),
    case("mixed_versions", r"return m.ip_network('10.0.0.0/8').subnet_of(m.ip_network('::/0'))"),
])

task("calendar", "leap_ranges", ["isleap", "leapdays", "monthrange", "_monthlen"], [
    case("leap_years", r"return [m.isleap(1900), m.isleap(2000), m.leapdays(1900,2001), m.leapdays(2001,1900)]"),
    case("month_ranges", r"return [list(m.monthrange(2024,2)), list(m.monthrange(1900,2)), list(m.monthrange(2023,12))]"),
    case("bad_month", r"return m.monthrange(2024,13)"),
])
task("calendar", "week_layout", ["Calendar.monthdayscalendar", "Calendar.iterweekdays", "Calendar.itermonthdays2"], [
    case("sunday_first", r"return m.Calendar(firstweekday=6).monthdayscalendar(2024,2)"),
    case("weekday_order", r"return [list(m.Calendar().iterweekdays()), list(m.Calendar(3).iterweekdays())]"),
    case("pairs", r"return [list(x) for x in m.Calendar().itermonthdays2(2021,2)][:9]"),
])
task("calendar", "date_boundaries", ["Calendar.itermonthdates", "Calendar.itermonthdays3", "_nextmonth", "_prevmonth"], [
    case("padding", r"ds=list(m.Calendar().itermonthdates(2024,3)); return [str(ds[0]), str(ds[-1]), len(ds)]"),
    case("year_9999_days3", r"return [list(t) for t in m.Calendar().itermonthdays3(9999,12)][-3:]"),
    case("year_9999_dates", r"return len(list(m.Calendar().itermonthdates(9999,12)))"),
])
task("calendar", "text_format", ["TextCalendar.formatmonth", "TextCalendar.formatweekheader", "TextCalendar.formatmonthname"], [
    case("month_w3", r"return m.TextCalendar(firstweekday=6).formatmonth(2024,2,w=3)"),
    case("narrow_header", r"return m.TextCalendar().formatweekheader(1)"),
    case("month_name", r"return m.TextCalendar().formatmonthname(2024,12,width=20,withyear=False)"),
])
task("calendar", "year_layout", ["Calendar.yeardayscalendar", "TextCalendar.formatyear", "weekday"], [
    case("rows", r"rows=m.Calendar().yeardayscalendar(2023,width=4); return [len(rows), len(rows[0]), rows[0][1][0]]"),
    case("year_text", r"return m.TextCalendar().formatyear(2023,w=2,l=1,c=3,m=4).splitlines()[:4]"),
    case("weekdays", r"return [int(m.weekday(2024,2,29)), int(m.weekday(1,1,1)), int(m.weekday(10000,1,1))]"),
])

task("pprint", "dicts", ["PrettyPrinter._pprint_dict", "PrettyPrinter._format_dict_items", "_safe_key"], [
    case("nested_width", r"return m.pformat({'b':1,'a':[1,2,3],'c':{'z':1,'y':2}},width=20)"),
    case("unsorted", r"return m.pformat({'b':1,'a':2},sort_dicts=False)"),
    case("mixed_keys", r"return m.pformat({3:'x','a':'y',None:'z'})"),
])
task("pprint", "sequences", ["PrettyPrinter._format_items", "PrettyPrinter._pprint_list"], [
    case("list_width", r"return m.pformat(list(range(30)),width=40)"),
    case("compact", r"return m.pformat(list(range(30)),width=40,compact=True)"),
    case("nested_indent", r"return m.pformat([[1,2],[3,4]]*3,width=15,indent=2)"),
])
task("pprint", "depth_recursion", ["PrettyPrinter._safe_repr", "saferepr", "isrecursive"], [
    case("depth", r"x=[1,[2,[3,[4]]]]; return [m.pformat(x,depth=2), m.pformat(x,depth=1)]"),
    case("recursive", r"a=[1]; a.append(a); return [m.saferepr(a), m.isrecursive(a), m.isreadable(a)]"),
    case("dict_depth", r"return m.pformat({'k':{'k':{'k':1}}},depth=2)"),
])
task("pprint", "strings_bytes", ["PrettyPrinter._pprint_str", "PrettyPrinter._pprint_bytes"], [
    case("long_str", r"return m.pformat('the quick brown fox jumps over the lazy dog '*2,width=30)"),
    case("long_bytes", r"return m.pformat(b'\x00\x01binary data that is fairly long here'*2,width=30)"),
    case("dict_value", r"return m.pformat({'key':'value '*10},width=30)"),
])
task("pprint", "numbers_options", ["PrettyPrinter.__init__", "PrettyPrinter._safe_repr"], [
    case("underscores", r"return [m.pformat(12345678,underscore_numbers=True), m.pformat([1234567.5, 10**10],underscore_numbers=True)]"),
    case("zero_width", r"return m.PrettyPrinter(width=0)"),
    case("tuples", r"return [m.pformat((1,),width=1), m.pformat(()), m.pformat((1,2),width=1)]"),
])


def build_manifest():
    assert len(TASKS) == 30
    tasks = []
    for item in TASKS:
        path, module = load_source(item["module"])
        tasks.append({**item, "source_path": str(path), "gold": {c["name"]: oracle(module, c["code"]) for c in item["cases"]}})
    return tasks


if __name__ == "__main__":
    print(json.dumps(build_manifest(), indent=2))
