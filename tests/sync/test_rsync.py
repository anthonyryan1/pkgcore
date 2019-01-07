# Copyright: 2006 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD

from pkgcore.sync import base, rsync
from tests.sync.syncer import make_bogus_syncer, make_valid_syncer
from snakeoil.test import TestCase

bogus = make_bogus_syncer(rsync.rsync_syncer)
valid = make_valid_syncer(rsync.rsync_syncer)


class TestRsyncSyncer(TestCase):

    def test_uri_parse(self):
        self.assertRaises(base.SyncError, bogus,
            "/tmp/foon", "rsync+hopefully_nonexistent_binary://foon.com/dar")
        o = valid("/tmp/foon", "rsync://dar/module")
        self.assertEqual(o.rsh, None)
        self.assertEqual(o.uri, "rsync://dar/module/")

        o = valid("/tmp/foon", "rsync+/bin/sh://dar/module")
        self.assertEqual(o.uri, "rsync://dar/module/")
        self.assertEqual(o.rsh, "/bin/sh")
