#
# Copyright (c) 2016 Red Hat, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import os
import git
import logging
import yaml

from pecan import conf  # noqa

logger = logging.getLogger(__name__)

RESOURCES_STRUCT = {'resources': {'rtype': {'key': {}}}}


class YAMLDBException(Exception):
    pass


class YAMLBackend(object):
    def __init__(self, git_repo_url, git_ref, sub_dir,
                 clone_path, cache_path):
        """ Class to read and validate resources from YAML
        files from stored in a GIT repository. The main data
        structure as well as resources structure must follow
        specific constraints.

        This Class also maintains a cache file to avoid full
        re-load and validation at init when ref hash has not
        changed.

        :param git_repo_url: The URI of the GIT repository
        :param git_ref: The GIT repository refs such as
               refs/zuul/master/Z3a46b05b4574472bbf093ff5562cba42
               or refs/heads/master
        :param sub_dir: The path from the GIT root to YAML files
        :param clone_path: The path where to clone the GIT repository
        :param cache_path: The path to the cached file
        """
        self.git_repo_url = git_repo_url
        self.git_ref = git_ref
        self.clone_path = clone_path
        self.cache_path = cache_path
        self.cache_path_hash = "%s_%s" % (cache_path, '_hash')
        self.db_path = os.path.join(self.clone_path, sub_dir)
        self.rids = {}
        self.refresh()

    def _get_repo_hash(self):
        repo = git.Git(self.clone_path)
        repo_hash = repo.execute(['git', '--no-pager', 'log', '-1',
                                  '--pretty=%h', 'HEAD'])
        return repo_hash

    def _get_cache_hash(self):
        return file(self.cache_path_hash).read().strip()

    def _update_cache(self):
        repo_hash = self._get_repo_hash()
        yaml.dump(self.data, file(self.cache_path, 'w'))
        file(self.cache_path_hash, 'w').write(repo_hash)
        logger.info("Cache file has been updated.")

    def _load_from_cache(self):
        if not os.path.isfile(self.cache_path_hash):
            logger.info("No DB cache file found.")
        else:
            repo_hash = self._get_repo_hash()
            cached_repo_hash = self._get_cache_hash()
            if cached_repo_hash == repo_hash:
                self.data = yaml.safe_load(file(self.cache_path))
                logger.info("Load data from the cache.")
            else:
                logger.info("DB cache is outdated.")

    def _update_git_clone(self):
        repo = git.Git(self.clone_path)
        repo.init()
        try:
            repo.execute(['git', 'remote', 'add',
                          'origin', self.git_repo_url])
        except Exception:
            logger.info("Re-using the previous repo path %s" % self.clone_path)
            repo.execute(['git', 'remote', 'remove', 'origin'])
            repo.execute(['git', 'remote', 'add',
                          'origin', self.git_repo_url])
            logger.info("Update the previous remote origin to %s." % (
                        self.git_repo_url))
        if self.git_ref != 'master' and not self.git_ref.startswith('refs/'):
            if self.git_ref == "master^1":
                # Keep that for compatibility SF < 2.4.0
                ref = 'FETCH_HEAD^1'
            else:
                # Here git_ref is a commit SHA or SHA^1
                ref = self.git_ref
            repo.execute(['git', 'fetch', 'origin', 'master'])
            repo.execute(['git', 'checkout', ref])
        else:
            repo.execute(['git', 'fetch', '-f', 'origin',
                          '%s:refs/remotes/origin/myref' % self.git_ref])
            repo.execute(['git', 'checkout', 'origin/myref'])
        logger.info("Updated GIT repo %s at ref %s." % (self.git_repo_url,
                                                        self.git_ref))

    def _load_db(self):
        def check_ext(f):
            return f.endswith('.yaml') or f.endswith('.yml')
        logger.info("Load data from the YAML files.")
        self.rids = {}
        yamlfiles = [f for f in os.listdir(self.db_path) if check_ext(f)]
        for f in yamlfiles:
            logger.info("Reading %s ..." % f)
            try:
                yaml_data = yaml.safe_load(
                    file(os.path.join(self.db_path, f)))
            except:
                raise YAMLDBException(
                    "YAML format corrupted in file %s" % (
                        os.path.join(self.db_path, f)))
            if not self.data:
                self.data = self.validate(yaml_data, self.rids)
            else:
                data_to_append = self.validate(yaml_data, self.rids)
                for rtype, resources in data_to_append['resources'].items():
                    if rtype not in self.data['resources']:
                        self.data['resources'][rtype] = {}
                    self.data['resources'][rtype].update(resources)

    @staticmethod
    def _validate_base_struct(data):
        try:
            assert isinstance(data, type(RESOURCES_STRUCT))
            assert isinstance(data['resources'],
                              type(RESOURCES_STRUCT['resources']))
        except (AssertionError, KeyError):
            raise YAMLDBException(
                "The main resource data structure is invalid")
        try:
            for rtype, resources in data['resources'].items():
                assert isinstance(
                    rtype, type(RESOURCES_STRUCT['resources'].keys()[0]))
                assert isinstance(
                    resources, type(RESOURCES_STRUCT['resources']['rtype']))
        except AssertionError:
            raise YAMLDBException(
                "Resource type %s structure is invalid" % rtype)
        try:
            for rtype, resources in data['resources'].items():
                for rid, resource in resources.items():
                    assert isinstance(rid, str)
                    assert isinstance(
                        resource,
                        type(RESOURCES_STRUCT['resources']['rtype']['key']))
        except AssertionError:
            raise YAMLDBException(
                "Resource %s of type %s is invalid" % (resource, rtype))

    @staticmethod
    def _validate_rid_unicity(data, rids):
        # Verify at YAML load time that duplicated resources key
        # are not present. To avoid overlapping of resources.
        # https://gist.github.com/pypt/94d747fe5180851196eb implements a
        # solution but seems difficult as it does not support the
        # safe_loader and usage of that loader is important to avoid
        # loading malicious yaml serialized objects.
        # Inside a single yaml file a reviewer should take care to
        # the duplicated keys issue and also look at the logs for
        # checking not expected detected changes.
        #
        # Nevertheless between two or more yaml files this check can be
        # implemented.
        for rtype, resources in data['resources'].items():
            for rid, resource in resources.items():
                rids.setdefault(rtype, {})
                if rid not in rids[rtype]:
                    rids[rtype][rid] = None
                else:
                    raise YAMLDBException(
                        "Duplicated resource ID detected for "
                        "resource type: %s id: %s" % (rtype, rid))

    def refresh(self):
        """ Reload of the YAML files.
        """
        self.data = None
        self._update_git_clone()
        self._load_from_cache()
        # Load from files. Cache is not up to date.
        if not self.data:
            self._load_db()
            self._update_cache()

    @staticmethod
    def validate(data, rids):
        """ Validate the resource data structure.
        """
        YAMLBackend._validate_base_struct(data)
        YAMLBackend._validate_rid_unicity(data, rids)
        return data

    def get_data(self):
        """ Return the full data structure.
        """
        return self.data
