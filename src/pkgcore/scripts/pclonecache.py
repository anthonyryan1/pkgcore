# Copyright: 2005-2011 Brian Harring <ferringb@gmail.com
# Copyright: 2006 Marien Zwart <marienz@gentoo.org>
# License: BSD/GPL2

"""repository cache clone utility"""

import time

from pkgcore.util import commandline

argparser = commandline.ArgumentParser(
    domain=False, description=__doc__, script=(__file__, __name__))
argparser.add_argument(
    "source", config_type='cache', priority=20,
    action=commandline.StoreConfigObject,
    help="source cache to copy data from")
argparser.add_argument(
    "target", config_type='cache', priority=21,
    action=commandline.StoreConfigObject, writable=True,
    help="target cache to update.  Must be writable.")


@argparser.bind_main_func
def main(options, out, err):
    if options.target.readonly:
        argparser.error(
            "can't update cache label '%s', it's marked readonly." %
            (options.target,))

    source, target = options.source, options.target
    if not target.autocommits:
        target.sync_rate = 1000
    if options.verbose:
        out.write("grabbing target's existing keys")
    valid = set()
    start = time.time()
    if options.verbose:
        for k, v in source.items():
            out.write(f"updating {k}")
            target[k] = v
            valid.add(k)
    else:
        for k, v in source.items():
            target[k] = v
            valid.add(k)

    for x in target.keys():
        if x not in valid:
            if options.verbose:
                out.write(f"deleting {x}")
            del target[x]

    if options.verbose:
        out.write("took %i seconds" % int(time.time() - start))
