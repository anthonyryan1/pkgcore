# Copyright: 2006-2011 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD

import os, shutil

from pkgcore.test import TestCase
from snakeoil.test.mixins import TempDirMixin
from snakeoil.osutils import pjoin, ensure_dirs
from snakeoil.compatibility import all

from pkgcore.ebuild import profiles
from pkgcore.ebuild.misc import chunked_data
from pkgcore.ebuild.atom import atom
from pkgcore.ebuild.cpv import CPV
from pkgcore.restrictions import packages, restriction

atrue = packages.AlwaysTrue

class ProfileNode(profiles.ProfileNode):
    # re-inherited to disable inst-caching
    pass

class profile_mixin(TempDirMixin):

    def mk_profile(self, profile_name):
        return self.mk_profiles({'name':profile_name})

    def mk_profiles(self, *profiles, **kwds):
        for x in os.listdir(self.dir):
            shutil.rmtree(pjoin(self.dir, x))
        for idx, vals in enumerate(profiles):
            name = str(vals.pop("name", idx))
            path = pjoin(self.dir, name)
            ensure_dirs(path)
            parent = vals.pop("parent", None)
            for fname, data in vals.iteritems():
                open(pjoin(path, fname), "w").write(data)

            if idx and not parent:
                parent = idx - 1

            if parent is not None:
                open(pjoin(path, "parent"), "w").write("../%s" % (parent,))
        if kwds:
            for key, val in kwds.iteritems():
                open(pjoin(self.dir, key), "w").write(val)

    def assertEqualChunks(self, given_mapping, desired_mapping):
        def f(chunk):
            return chunked_data(chunk.key, tuple(set(chunk.neg)), tuple(set(chunk.pos)))
        given_mapping.optimize()
        return self._assertEqualPayload(given_mapping.render_to_dict(), desired_mapping, f, chunked_data)

    def assertEqualPayload(self, given_mapping, desired_mapping):
        def f(chunk):
            return chunked_data(chunk.restrict, tuple(sorted(chunk.data)))

        return self._assertEqualPayload(given_mapping, desired_mapping, f, chunked_data)

    assertEqualPayload = assertEqualChunks

    def _assertEqualPayload(self, given_mapping, desired_mapping, reformat_f,
        bare_kls):
        keys1, keys2 = set(given_mapping), set(desired_mapping)
        self.assertEqual(keys1, keys2, msg="keys differ: wanted %r got %r\nfrom %r" %
            (keys2, keys1, given_mapping))

        for key, desired in desired_mapping.iteritems():
            got = given_mapping[key]
            # sanity check the desired data, occasionally screw this up
            self.assertNotInstance(desired, bare_kls, msg="key %r, bad test invocation; "
                "bare %s instead of a tuple; val %r" % (key, bare_kls.__name__, got))
            self.assertInstance(got, tuple, msg="key %r, non tuple: %r" %
                (key, got))
            self.assertNotInstance(got, bare_kls, msg="key %r, bare %s, "
                "rather than tuple: %r" % (key, bare_kls.__name__, got))
            if not all(isinstance(x, bare_kls) for x in got):
                self.fail("non %s instance: key %r, val %r; types %r" % (bare_kls.__name__,
                    key, got, map(type, got)))
            got2, desired2 = tuple(map(reformat_f, got)), tuple(map(reformat_f, desired))
            self.assertEqual(got2, desired2, msg="key %r isn't equal; wanted %r, got %r" % (key, desired2, got2))



empty = ((), ())

class TestProfileNode(profile_mixin, TestCase):

    def setUp(self, default=True):
        TempDirMixin.setUp(self)
        if default:
            self.profile = "default"
            self.mk_profile(self.profile)

    def write_file(self, filename, iterable, profile=None):
        if profile is None:
            profile = self.profile
        open(pjoin(self.dir, profile, filename), "w").write(iterable)

    def parsing_checks(self, filename, attr, data="", line_negation=True):
        path = pjoin(self.dir, self.profile)
        self.write_file(filename, data)
        getattr(ProfileNode(path), attr)
        self.write_file(filename,  "-")
        self.assertRaises(profiles.ProfileError,
            getattr, ProfileNode(path), attr)

    def simple_eapi_awareness_check(self, filename, attr,
        bad_data="dev-util/diffball\ndev-util/bsdiff:1",
        good_data="dev-util/diffball\ndev-util/bsdiff"):
        # validate unset eapi=0 prior
        self.parsing_checks(filename, attr, data=good_data)
        self.write_file("eapi", "1")
        self.parsing_checks(filename, attr, data=good_data)
        self.parsing_checks(filename, attr, data=bad_data)
        self.write_file("eapi", "0")
        self.assertRaises(profiles.ProfileError,
            self.parsing_checks, filename, attr, data=bad_data)

    def test_eapi(self):
        path = pjoin(self.dir, self.profile)
        self.assertEqual(ProfileNode(path).eapi, '0')
        self.write_file("eapi", "1")
        self.assertEqual(ProfileNode(path).eapi, '1')
        self.write_file("eapi", "some-random-eapi-adsfafa")
        self.assertRaises(profiles.ProfileError, getattr,
            ProfileNode(path), 'eapi')

    def test_packages(self):
        p = ProfileNode(pjoin(self.dir, self.profile))
        self.assertEqual(p.system, empty)
        self.assertEqual(p.visibility, empty)
        self.parsing_checks("packages", "system")
        self.write_file("packages", "#foo\n")
        p = ProfileNode(pjoin(self.dir, self.profile))
        self.assertEqual(p.visibility, empty)
        self.assertEqual(p.system, empty)
        self.write_file("packages", "#foo\ndev-util/diffball\n")
        p = ProfileNode(pjoin(self.dir, self.profile))
        self.assertEqual(p.system, empty)
        self.assertEqual(list(p.visibility), [(), (atom("dev-util/diffball",
            negate_vers=True),)])

        self.write_file("packages", "-dev-util/diffball\ndev-foo/bar\n*dev-sys/atom\n"
            "-*dev-sys/atom2\nlock-foo/dar")
        p = ProfileNode(pjoin(self.dir, self.profile))
        self.assertEqual(p.system, ((atom("dev-sys/atom2"),), (atom("dev-sys/atom"),)))
        self.assertEqual([set(x) for x in p.visibility],
            [set([atom("dev-util/diffball", negate_vers=True)]),
            set([atom("dev-foo/bar", negate_vers=True),
                atom("lock-foo/dar", negate_vers=True)])
            ])
        self.simple_eapi_awareness_check('packages', 'system')

    def test_deprecated(self):
        self.assertEqual(ProfileNode(pjoin(self.dir, self.profile)).deprecated,
            None)
        self.write_file("deprecated", "")
        self.assertRaises(profiles.ProfileError, getattr,
            ProfileNode(pjoin(self.dir, self.profile)), "deprecated")
        self.write_file("deprecated", "foon\n#dar\nfasd")
        self.assertEqual(list(ProfileNode(pjoin(self.dir,
            self.profile)).deprecated),
            ["foon", "dar\nfasd"])

    def test_pkg_provided(self):
        self.assertEqual(ProfileNode(pjoin(self.dir,
            self.profile)).pkg_provided,
            ((), ()))
        self.parsing_checks("package.provided", "pkg_provided")
        self.write_file("package.provided", "-dev-util/diffball-1.0")
        self.assertEqual(ProfileNode(pjoin(self.dir,
            self.profile)).pkg_provided,
                ((CPV.versioned("dev-util/diffball-1.0"),), ()))
        self.write_file("package.provided", "dev-util/diffball-1.0")
        self.assertEqual(ProfileNode(pjoin(self.dir,
            self.profile)).pkg_provided, ((),
                (CPV.versioned("dev-util/diffball-1.0"),)))

    def test_masks(self):
        path = pjoin(self.dir, self.profile)
        self.assertEqual(ProfileNode(path).masks, empty)
        self.parsing_checks("package.mask", "masks")
        self.write_file("package.mask", "dev-util/diffball")
        self.assertEqual(ProfileNode(path).masks, ((),
            (atom("dev-util/diffball"),)))
        self.write_file("package.mask", "-dev-util/diffball")
        self.assertEqual(ProfileNode(path).masks,
            ((atom("dev-util/diffball"),), ()))
        self.simple_eapi_awareness_check('package.mask', 'masks')

    def _check_package_use_files(self, path, filename, attr):
        self.write_file(filename, "dev-util/bar X")
        self.assertEqualChunks(getattr(ProfileNode(path), attr),
           {"dev-util/bar":(chunked_data(atom("dev-util/bar"), (), ('X',)),)})
        self.write_file(filename, "-dev-util/bar X")
        self.assertRaises(profiles.ProfileError, getattr, ProfileNode(path),
            attr)

        # verify collapsing optimizations
        self.write_file(filename, "dev-util/foo X\ndev-util/foo X")
        self.assertEqualChunks(getattr(ProfileNode(path), attr),
            {"dev-util/foo":(chunked_data(atom("dev-util/foo"), (), ('X',)),)})

        self.write_file(filename, "d-u/a X\n=d-u/a-1 X")
        self.assertEqualChunks(getattr(ProfileNode(path), attr),
            {"d-u/a":(chunked_data(atom("d-u/a"), (), ('X',)),)})

        self.write_file(filename, "d-u/a X\n=d-u/a-1 -X")
        self.assertEqualChunks(getattr(ProfileNode(path), attr),
            {"d-u/a":(chunked_data(atom("d-u/a"), (), ('X',)),
                chunked_data(atom("=d-u/a-1"), ('X',), ()),)})

        self.write_file(filename, "=d-u/a-1 X\nd-u/a X")
        self.assertEqualChunks(getattr(ProfileNode(path), attr),
            {"d-u/a":(chunked_data(atom("d-u/a"), (), ('X',)),)})

        self.write_file(filename, "dev-util/bar -X\ndev-util/foo X")
        self.assertEqualChunks(getattr(ProfileNode(path), attr),
           {"dev-util/bar":(chunked_data(atom("dev-util/bar"), ('X',), ()),),
           "dev-util/foo":(chunked_data(atom("dev-util/foo"), (), ('X',)),)})

    def test_masked_use(self):
        path = pjoin(self.dir, self.profile)
        self.assertEqualChunks(ProfileNode(path).masked_use, {})
        self.parsing_checks("package.use.mask", "masked_use")
        if os.path.exists(pjoin(path, "package.use.mask")):
            os.unlink(pjoin(path, "package.use.mask"))
        self.parsing_checks("use.mask", "masked_use")
        self.write_file("use.mask", "")

        self._check_package_use_files(path, "package.use.mask", 'masked_use')

        self.write_file("use.mask", "mmx")
        self.assertEqualChunks(ProfileNode(path).masked_use,
            {"dev-util/bar":
                (chunked_data(atom("dev-util/bar"), ('X',), ('mmx',)),),
            "dev-util/foo":
                (chunked_data(atom("dev-util/foo"), (), ('X', 'mmx')),),
            atrue:(chunked_data(packages.AlwaysTrue, (), ("mmx",)),)
            })

        self.write_file("use.mask", "mmx\n-foon")
        self.assertEqualChunks(ProfileNode(path).masked_use,
            {"dev-util/bar":
                (chunked_data(atom("dev-util/bar"), ('X', 'foon'), ('mmx',)),),
            "dev-util/foo":
                (chunked_data(atom("dev-util/foo"), ('foon',), ('X', 'mmx',)),),
            atrue:(chunked_data(packages.AlwaysTrue, ('foon',), ('mmx',)),)
            })

        # verify that use.mask is layered first, then package.use.mask
        self.write_file("package.use.mask", "dev-util/bar -mmx foon")
        self.assertEqualChunks(ProfileNode(path).masked_use,
            {atrue:(chunked_data(atrue, ('foon',), ('mmx',)),),
            "dev-util/bar":(chunked_data(atom("dev-util/bar"), ('mmx',), ('foon',)),)
            })

        self.write_file("package.use.mask", "")
        self.assertEqualChunks(ProfileNode(path).masked_use,
           {atrue:(chunked_data(atrue, ('foon',),('mmx',)),)})
        self.simple_eapi_awareness_check('package.use.mask', 'masked_use',
            bad_data='=de/bs-1:1 x\nda/bs y',
            good_data='=de/bs-1 x\nda/bs y')

        self.write_file("package.use.mask", "dev-util/diffball")
        self.assertRaises(profiles.ProfileError, getattr,
            ProfileNode(path), 'masked_use')

    def test_forced_use(self):
        path = pjoin(self.dir, self.profile)
        self.assertEqualChunks(ProfileNode(path).forced_use, {})
        self.parsing_checks("package.use.force", "forced_use")
        if os.path.exists(pjoin(path, "package.use.force")):
            os.unlink(pjoin(path, "package.use.force"))
        self.parsing_checks("use.force", "forced_use")
        self.write_file("use.force", "")

        self._check_package_use_files(path, "package.use.force", 'forced_use')

        self.write_file("use.force", "mmx")

        self.assertEqualChunks(ProfileNode(path).forced_use,
            {"dev-util/bar":
                (chunked_data(atom("dev-util/bar"), ('X',), ('mmx',)),),
            "dev-util/foo":
                (chunked_data(atom("dev-util/foo"), (), ('X', 'mmx')),),
            atrue:(chunked_data(atrue, (), ('mmx',)),),
            })

        self.write_file("use.force", "mmx\n-foon")
        self.assertEqualChunks(ProfileNode(path).forced_use,
            {"dev-util/bar":
                (chunked_data(atom("dev-util/bar"), ('X', 'foon',), ('mmx',)),),
            "dev-util/foo":
                (chunked_data(atom("dev-util/foo"), ('foon',), ('X', 'mmx')),),
            atrue:(chunked_data(atrue, ('foon',), ('mmx',)),)
            })

        # verify that use.force is layered first, then package.use.force
        self.write_file("package.use.force", "dev-util/bar -mmx foon")
        self.assertEqualChunks(ProfileNode(path).forced_use,
            {atrue:(chunked_data(atrue, ('foon',), ('mmx',)),),
            "dev-util/bar":(chunked_data(atom("dev-util/bar"), ('mmx',), ('foon',)),)
            })

        self.write_file("package.use.force", "")
        self.assertEqualChunks(ProfileNode(path).forced_use,
            {atrue:(chunked_data(atrue, ('foon',), ('mmx',)),)
            })
        self.simple_eapi_awareness_check('package.use.force', 'forced_use',
            bad_data='=de/bs-1:1 x\nda/bs y',
            good_data='=de/bs-1 x\nda/bs y')

        self.write_file("package.use.force", "dev-util/diffball")
        self.assertRaises(profiles.ProfileError, getattr,
            ProfileNode(path), 'forced_use')

    def test_pkg_use(self):
        path = pjoin(self.dir, self.profile)
        self.assertEqualChunks(ProfileNode(path).pkg_use, {})
        self.parsing_checks("package.use", "pkg_use")
        self.write_file("package.use", "dev-util/bar X")
        self.assertEqualChunks(ProfileNode(path).pkg_use,
            {"dev-util/bar":(chunked_data(atom("dev-util/bar"), (), ('X',)),)})
        self.write_file("package.use", "-dev-util/bar X")
        self.assertRaises(profiles.ProfileError, getattr, ProfileNode(path),
            "pkg_use")

        self._check_package_use_files(path, "package.use", 'pkg_use')

        self.write_file("package.use", "dev-util/bar -X\ndev-util/foo X")
        self.assertEqualChunks(ProfileNode(path).pkg_use,
            {"dev-util/bar": (chunked_data(atom("dev-util/bar"), ('X',), ()),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), (), ('X',)),)})
        self.simple_eapi_awareness_check('package.use', 'pkg_use',
            bad_data='=de/bs-1:1 x\nda/bs y',
            good_data='=de/bs-1 x\nda/bs y')

        self.write_file("package.use", "dev-util/diffball")
        self.assertRaises(profiles.ProfileError, getattr,
            ProfileNode(path), 'pkg_use')

    def test_parents(self):
        path = pjoin(self.dir, self.profile)
        os.mkdir(pjoin(path, 'child'))
        self.write_file("parent", "..", profile="%s/child" % self.profile)
        p = ProfileNode(pjoin(path, "child"))
        self.assertEqual(1, len(p.parents))
        self.assertEqual(p.parents[0].path, path)

    def test_virtuals(self):
        path = pjoin(self.dir, self.profile)
        self.assertEqual(ProfileNode(path).virtuals, {})
        self.parsing_checks("virtuals", "virtuals")
        self.write_file("virtuals", "virtual/alsa media-sound/alsalib")
        self.assertEqual(ProfileNode(path).virtuals,
            {'alsa':atom("media-sound/alsalib")})
        self.simple_eapi_awareness_check('virtuals', 'virtuals',
            bad_data='virtual/al =de/bs-1:1\nvirtual/foo da/bs',
            good_data='virtual/al =de/bs-1\nvirtual/foo da/bs')

    def test_default_env(self):
        path = pjoin(self.dir, self.profile)
        self.assertEqual(ProfileNode(path).default_env, {})
        self.write_file("make.defaults", "X=foo\n")
        self.assertEqual(ProfileNode(path).default_env, {'X':'foo'})
        self.write_file('make.defaults', 'y=narf\nx=${y}\n')
        self.assertEqual(ProfileNode(path).default_env,
            {'y':'narf', 'x':'narf'})

    def test_bashrc(self):
        path = pjoin(self.dir, self.profile)
        self.assertIdentical(ProfileNode(path).bashrc, None)
        self.write_file("profile.bashrc", '')
        self.assertNotEqual(ProfileNode(path).bashrc, None)


class TestOnDiskProfile(profile_mixin, TestCase):

    # use a derivative, using the inst caching disabled ProfileNode kls
    # from above
    class kls(profiles.OnDiskProfile):
        _node_kls = ProfileNode

    def get_profile(self, profile, **kwds):
        return self.kls(self.dir, profile, **kwds)

    def test_stacking(self):
        self.mk_profiles(
            {},
            {}
        )
        base = self.get_profile("0")
        self.assertEqual([x.path for x in base.stack],
            [self.dir, pjoin(self.dir, "0")])
        self.assertEqual(len(base.system), 0)
        self.assertEqual(len(base.masks), 0)
        self.assertEqual(base.virtuals, {})
        self.assertEqual(base.default_env, {})
        self.assertFalse(base.masked_use)
        self.assertFalse(base.forced_use)
        self.assertEqual(len(base.bashrc), 0)

    def test_packages(self):
        self.mk_profiles(
            {"packages":"*dev-util/diffball\ndev-util/foo\ndev-util/foo2\n"},
            {"packages":"*dev-util/foo\n-*dev-util/diffball\n-dev-util/foo2\n"}
        )
        p = self.get_profile("0")
        self.assertEqual(sorted(p.system), sorted([atom("dev-util/diffball")]))
        self.assertEqual(sorted(p.masks),
            sorted(atom("dev-util/foo%s" % x, negate_vers=True) for x in ['', '2']))

        p = self.get_profile("1")
        self.assertEqual(sorted(p.system), sorted([atom("dev-util/foo")]))
        self.assertEqual(sorted(p.masks),
            [atom("dev-util/foo", negate_vers=True)])

    def test_masks(self):
        self.mk_profiles(
            {"package.mask":"dev-util/foo"},
            {},
            {"package.mask":"-dev-util/confcache\ndev-util/foo"},
            **{"package.mask":"dev-util/confcache"}
        )
        self.assertEqual(sorted(self.get_profile("0").masks),
            sorted(atom("dev-util/" + x) for x in ["confcache", "foo"]))
        self.assertEqual(sorted(self.get_profile("1").masks),
            sorted(atom("dev-util/" + x) for x in ["confcache", "foo"]))
        self.assertEqual(sorted(self.get_profile("2").masks),
            [atom("dev-util/foo")])

    def test_bashrc(self):
        self.mk_profiles(
            {"profile.bashrc":""},
            {},
            {"profile.bashrc":""}
        )
        self.assertEqual(len(self.get_profile("0").bashrc), 1)
        self.assertEqual(len(self.get_profile("1").bashrc), 1)
        self.assertEqual(len(self.get_profile("2").bashrc), 2)

    def test_virtuals(self):
        self.mk_profiles(
            {"virtuals":"virtual/alsa\tdev-util/foo1\nvirtual/blah\tdev-util/blah"},
            {},
            {"virtuals":"virtual/alsa\tdev-util/foo2\nvirtual/dar\tdev-util/foo2"}
        )
        self.assertEqual(sorted(self.get_profile("0").virtuals.iteritems()),
            sorted([("alsa", atom("dev-util/foo1")), ("blah", atom("dev-util/blah"))]))
        self.assertEqual(sorted(self.get_profile("1").virtuals.iteritems()),
            sorted([("alsa", atom("dev-util/foo1")), ("blah", atom("dev-util/blah"))]))
        self.assertEqual(sorted(self.get_profile("2").virtuals.iteritems()),
            sorted([("alsa", atom("dev-util/foo2")), ("blah", atom("dev-util/blah")),
                ("dar", atom("dev-util/foo2"))]))

    def test_masked_use(self):
        self.mk_profiles({})
        self.assertEqualPayload(self.get_profile("0").masked_use, {})

        self.mk_profiles(
            {"use.mask":"X\nmmx\n"},
            {},
            {"use.mask":"-X"})

        self.assertEqualPayload(self.get_profile("0").masked_use,
            {atrue:(chunked_data(atrue, (), ('X', 'mmx')),)})

        self.assertEqualPayload(self.get_profile("1").masked_use,
            {atrue:(chunked_data(atrue, (), ('X', 'mmx',)),)})

        self.assertEqualPayload(self.get_profile("2").masked_use,
            {atrue:(chunked_data(atrue, ('X',), ('mmx',)),)})


        self.mk_profiles(
            {"use.mask":"X\nmmx\n", "package.use.mask":"dev-util/foo cups"},
            {"package.use.mask": "dev-util/foo -cups"},
            {"use.mask":"-X", "package.use.mask": "dev-util/blah X"})

        self.assertEqualPayload(self.get_profile("0").masked_use,
            {atrue:(chunked_data(atrue, (), ('X', 'mmx')),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), (), ("X", "cups", "mmx")),),
            })

        self.assertEqualPayload(self.get_profile("1").masked_use,
            {atrue:(chunked_data(atrue, (), ('X', 'mmx')),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), ('cups',), ("X", "mmx")),),
            })

        self.assertEqualPayload(self.get_profile("2").masked_use,
            {atrue:(chunked_data(atrue, ('X',), ('mmx',)),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), ('X', 'cups'), ("mmx",)),),
            "dev-util/blah":(chunked_data(atom("dev-util/blah"), (), ("X", "mmx",)),)
            })


        self.mk_profiles(
            {"use.mask":"X", "package.use.mask":"dev-util/foo -X"},
            {"use.mask":"X"},
            {"package.use.mask":"dev-util/foo -X"})

        self.assertEqualPayload(self.get_profile("0").masked_use,
            {atrue:(chunked_data(atrue, (), ("X",)),),
            "dev-util/foo": (chunked_data(atom("dev-util/foo"), ('X',), ()),)
            })
        self.assertEqualPayload(self.get_profile("1").masked_use,
            {atrue:(chunked_data(atrue, (), ("X",)),),
            "dev-util/foo": (chunked_data(atom("dev-util/foo"), (), ("X",)),)
            })
        self.assertEqualPayload(self.get_profile("2").masked_use,
            {atrue:(chunked_data(atrue, (), ("X")),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), ("X",), (),),)
            })

        # pkgcore bug 237; per PMS, later profiles can punch wholes in the
        # ranges applicable.
        self.mk_profiles(
            {"package.use.mask":"dev-util/foo X"},
            {"package.use.mask":">=dev-util/foo-1 -X"},
            {"package.use.mask":">=dev-util/foo-2 X"},
            {"package.use.mask":"dev-util/foo X", "name":"collapse_p"},
            {"package.use.mask":"dev-util/foo -X", "parent":"2", "name":"collapse_n"},
            )

        self.assertEqualPayload(self.get_profile("collapse_p").masked_use,
            {"dev-util/foo":(chunked_data(atom("dev-util/foo"), (), ("X",)),)
            })

        self.assertEqualPayload(self.get_profile("collapse_n").masked_use,
            {"dev-util/foo":(chunked_data(atom("dev-util/foo"), ("X",), (),),),
            })

    def test_forced_use(self):
        self.mk_profiles({})
        self.assertEqualPayload(self.get_profile("0").forced_use, {})
        self.mk_profiles(
            {"use.force":"X\nmmx\n"},
            {},
            {"use.force":"-X"})

        self.assertEqualPayload(self.get_profile("0").forced_use,
            {atrue:(chunked_data(atrue, (), ('X', 'mmx')),)})
        self.assertEqualPayload(self.get_profile("1").forced_use,
            {atrue:(chunked_data(atrue, (), ('X', 'mmx')),)})
        self.assertEqualPayload(self.get_profile("2").forced_use,
            {atrue:(chunked_data(atrue, ('X',), ('mmx',)),)})

        self.mk_profiles(
            {"use.force":"X\nmmx\n", "package.use.force":"dev-util/foo cups"},
            {"package.use.force": "dev-util/foo -cups"},
            {"use.force":"-X", "package.use.force": "dev-util/blah X"})

        self.assertEqualPayload(self.get_profile("0").forced_use,
            {atrue:(chunked_data(atrue, (), ('X', 'mmx')),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), (), ("X", "mmx", "cups",)),),
            })
        self.assertEqualPayload(self.get_profile("1").forced_use,
            {atrue:(chunked_data(atrue, (), ('X', 'mmx')),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), ('cups',), ("X", "mmx")),),
            })
        self.assertEqualPayload(self.get_profile("2").forced_use,
            {atrue:(chunked_data(atrue, ('X',), ('mmx',)),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), ('cups', 'X'), ('mmx',)),),
            "dev-util/blah":(chunked_data(atom("dev-util/blah"), (), ('X', "mmx")),),
            })

        self.mk_profiles(
            {"use.force":"X", "package.use.force":"dev-util/foo -X"},
            {"use.force":"X"},
            {"package.use.force":"dev-util/foo -X"})

        self.assertEqualPayload(self.get_profile("0").forced_use,
            {atrue:(chunked_data(atrue, (), ("X",)),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), ('X',), ()),),
            })
        self.assertEqualPayload(self.get_profile("1").forced_use,
            {atrue:(chunked_data(atrue, (), ("X",)),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), (), ('X',)),),
            })
        self.assertEqualPayload(self.get_profile("2").forced_use,
            {atrue:(chunked_data(atrue, (), ("X",)),),
            "dev-util/foo":(chunked_data(atom("dev-util/foo"), ('X',), ()),),
            })

    def test_pkg_use(self):
        self.mk_profiles({})
        self.assertEqualPayload(self.get_profile("0").pkg_use, {})
        self.mk_profiles(
            {"package.use":"dev-util/bsdiff X mmx\n"},
            {},
            {"package.use":"dev-util/bsdiff -X\n"},
            {"package.use":"dev-util/bsdiff -mmx\ndev-util/diffball X"},
            {"package.use":"dev-util/bsdiff X\ndev-util/diffball -X\n"}
            )

        self.assertEqualPayload(self.get_profile("0").pkg_use,
            {'dev-util/bsdiff':
                (chunked_data(atom("dev-util/bsdiff"), (), ('X', 'mmx')),)
            })
        self.assertEqualPayload(self.get_profile("1").pkg_use,
            {'dev-util/bsdiff':
                (chunked_data(atom("dev-util/bsdiff"), (), ('X', 'mmx')),)
            })
        self.assertEqualPayload(self.get_profile("2").pkg_use,
            {'dev-util/bsdiff':
                (chunked_data(atom("dev-util/bsdiff"), ('X',), ('mmx',)),)
            })
        self.assertEqualPayload(self.get_profile("3").pkg_use,
            {'dev-util/diffball':
                (chunked_data(atom("dev-util/diffball"), (), ('X',)),),
            'dev-util/bsdiff':
                (chunked_data(atom("dev-util/bsdiff"), ('X', 'mmx'), ()),),
            })
        self.assertEqualPayload(self.get_profile("4").pkg_use,
            {'dev-util/diffball':
                (chunked_data(atom("dev-util/diffball"), ('X',), ()),),
            'dev-util/bsdiff':
                (chunked_data(atom("dev-util/bsdiff"), ('mmx',), ('X',)),),
            })

    def test_default_env(self):
        self.mk_profiles({})
        self.assertEqual(self.get_profile("0").default_env, {})
        self.mk_profiles(
            {"make.defaults":"X=y\n"},
            {},
            {"make.defaults":"X=-y\nY=foo\n"})
        self.assertEqual(self.get_profile('0',
            incrementals=['X']).default_env,
           {'X':tuple('y')})
        self.assertEqual(self.get_profile('1',
            incrementals=['X']).default_env,
           {'X':tuple('y')})
        self.assertEqual(self.get_profile('2',
            incrementals=['X']).default_env,
           {'Y':'foo'})

    def test_provides_repo(self):
        self.mk_profiles({})
        self.assertEqual(len(self.get_profile("0").provides_repo), 0)

        self.mk_profiles(
            {"package.provided":"dev-util/diffball-0.7.1"})
        self.assertEqual([x.cpvstr for x in
            self.get_profile("0").provides_repo],
            ["dev-util/diffball-0.7.1"])

        self.mk_profiles(
            {"package.provided":"dev-util/diffball-0.7.1"},
            {"package.provided":
                "-dev-util/diffball-0.7.1\ndev-util/bsdiff-0.4"}
        )
        self.assertEqual([x.cpvstr for x in
            sorted(self.get_profile("1").provides_repo)],
            ["dev-util/bsdiff-0.4"])

    def test_deprecated(self):
        self.mk_profiles({})
        self.assertFalse(self.get_profile("0").deprecated)
        self.mk_profiles(
            {"deprecated":"replacement\nfoon\n"},
            {}
            )
        self.assertFalse(self.get_profile("1").deprecated)
        self.mk_profiles(
            {},
            {"deprecated":"replacement\nfoon\n"}
            )
        self.assertTrue(self.get_profile("1").deprecated)
