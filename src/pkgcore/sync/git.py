# Copyright: 2006 Brian Harring <ferringb@gmail.com>
# License: GPL2/BSD

__all__ = ("git_syncer",)

import os

from pkgcore.sync import base


class git_syncer(base.VcsSyncer):

    binary = "git"

    supported_uris = (
        ('git://', 5),
        ('git+', 5),
    )

    @classmethod
    def is_usable_on_filepath(cls, path):
        git_path = os.path.join(path, '.git')
        if cls.disabled or not os.path.isdir(git_path):
            return None
        # defer to git-svn plugin
        if os.path.isdir(os.path.join(git_path, 'svn')):
            return None
        return (cls._rewrite_uri_from_stat(git_path, "git://"),)

    @staticmethod
    def parse_uri(raw_uri):
        if not raw_uri.startswith("git+") and not raw_uri.startswith("git://"):
            raise base.UriError(
                raw_uri, "doesn't start with git+ nor git://")
        if raw_uri.startswith("git+"):
            if raw_uri.startswith("git+:"):
                raise base.UriError(
                    raw_uri, "need to specify the sub protocol if using git+")
            return raw_uri[4:]
        return raw_uri

    def __init__(self, basedir, uri, **kwargs):
        uri = self.parse_uri(uri)
        super().__init__(basedir, uri, **kwargs)

    def _initial_pull(self):
        return [self.binary_path, "clone", self.uri, self.basedir]

    def _update_existing(self):
        return [self.binary_path, "pull"]
