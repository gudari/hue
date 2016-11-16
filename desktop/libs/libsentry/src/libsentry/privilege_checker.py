#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging

from collections import defaultdict

from libsentry.api import get_api as get_api_v1
from libsentry.api2 import get_api as get_api_v2


LOG = logging.getLogger(__name__)


PRIVILEGE_HIERARCHY = {
  'SELECT': 0,
  'QUERY': 0,
  'READ': 0,  # Not a Sentry privilege, but enables v1 and v2 cross-compatible action type
  'INSERT': 1,
  'UPDATE': 1,
  'WRITE': 1,  # Not a Sentry privilege, but enables v1 and v2 cross-compatible action type
  'ALL': 2
}

SENTRY_OBJECTS = (
  'SERVER',
  'DB',
  'TABLE',
  'COLUMN',
  'COLLECTION',
  'CONFIG',
  'URI'
)

SENTRY_PRIVILEGE_KEY = 'SENTRY_PRIVILEGE'


class PrivilegeChecker(object):
  """
  Given a user, checks and applies Sentry privilege and authorization rules against Sentry objects
  """

  def __init__(self, user, api_v1=None, api_v2=None):
    self.user = user
    self.api_v1 = api_v1 if api_v1 else get_api_v1(self.user)
    self.api_v2 = api_v2 if api_v2 else get_api_v2(self.user, component='solr')


  def filter_objects(self, authorizableSet, action='READ'):
    """
    Given a set of authorizable Sentry objects and a requested action, return a filtered set of objects that the user
    has privileges to perform the given action upon.
    :param authorizableSet: a list of Sentry authorizable objects which can consist of V1 or V2 objects:
      V1 - {'column': 'total_emp', 'table': 'sample_08', 'db': 'default', 'server': 'server1'}
      V2 - {'component': 'solr', 'serviceName': 'server1', 'type': 'COLLECTION', 'name': 'twitter_demo'}
    :param action: requested action-level that we should check privileges against (default: READ)
    """
    action = action.upper()
    filtered_objects = []
    privileges = []

    # Separate V1 (Hive) and V2 (Solr) authorizable objects
    v1_authorizables = [obj for obj in authorizableSet if 'db' in obj]
    v2_authorizables = [obj for obj in authorizableSet if 'component' in obj]

    if v1_authorizables:
      user_roles = self.api_v1.list_sentry_roles_by_group('*')  # Get all roles for user
      for role in user_roles:
        role_privileges = self.api_v1.list_sentry_privileges_by_role(role['name'])
        privileges.extend(role_privileges)  # This may result in duplicates but will get reduced in hierarchy tree

      privilege_hierarchy = self._to_privilege_hierarchy_v1(privileges)

      for authorizable in v1_authorizables:
        if self._is_object_action_authorized_v1(hierarchy=privilege_hierarchy, object=authorizable, action=action):
          filtered_objects.append(authorizable)

    if v2_authorizables:
      privileges = []
      user_roles = self.api_v2.list_sentry_roles_by_group('*')  # Get all roles for user
      for role in user_roles:
        role_privileges = self.api_v2.list_sentry_privileges_by_role(role['name'])
        privileges.extend(role_privileges)

        privilege_hierarchy = self._to_privilege_hierarchy_v2(privileges)

        for authorizable in v2_authorizables:
          if self._is_object_action_authorized_v2(hierarchy=privilege_hierarchy, object=authorizable, action=action):
            filtered_objects.append(authorizable)

    return filtered_objects


  def _to_privilege_hierarchy_v1(self, privileges):
    """
    Converts a list of privileges to a hierarchical tree of privileges by object, where the privilege is stored into a
    key named SENTRY_PRIVILEGE_KEY.
    NOTE: This assumes no objects share the same name as SENTRY_PRIVILEGE_KEY
    """
    tree = lambda: defaultdict(tree)
    hierarchy = tree()

    for privilege in privileges:
      column, table, database, server = privilege['column'], privilege['table'], privilege['database'], privilege['server']
      if column:
        hierarchy[server][database][table][column][SENTRY_PRIVILEGE_KEY] = privilege
      elif table:
        hierarchy[server][database][table][SENTRY_PRIVILEGE_KEY] = privilege
      elif database:
        hierarchy[server][database][SENTRY_PRIVILEGE_KEY] = privilege
      else:
        hierarchy[server][SENTRY_PRIVILEGE_KEY] = privilege

    return hierarchy


  def _to_privilege_hierarchy_v2(self, privileges):
    """
    Converts a list of privileges to a hierarchical tree of privileges by object, where the privilege is stored into a
    key named SENTRY_PRIVILEGE_KEY.
    NOTE: This assumes no objects share the same name as SENTRY_PRIVILEGE_KEY
    """
    tree = lambda: defaultdict(tree)
    hierarchy = tree()

    for privilege in privileges:
      component, service, authorizables = privilege['component'], privilege['serviceName'], privilege['authorizables']
      for obj in authorizables:
        object_type, object_name = obj.get('type'), obj.get('name')
        hierarchy[component][service][object_type][object_name][SENTRY_PRIVILEGE_KEY] = privilege

    return hierarchy


  def _is_object_action_authorized_v1(self, hierarchy, object, action='READ'):
    requested_action_level = PRIVILEGE_HIERARCHY[action]

    # Initialize all privileges for all object levels to non-authorized by default
    privileges_applied = dict((obj, -1) for obj in SENTRY_OBJECTS)

    server, db, table, column = object['server'], object['db'], object['table'], object['column']

    if server:  # Get server-level privilege
      if server in hierarchy:
        if SENTRY_PRIVILEGE_KEY in hierarchy[server]:
          privileges_applied['SERVER'] = PRIVILEGE_HIERARCHY[hierarchy[server][SENTRY_PRIVILEGE_KEY]['action']]
        if db and db in hierarchy[server]: # Get db-level privilege
          if SENTRY_PRIVILEGE_KEY in hierarchy[server][db]:
            privileges_applied['DB'] = PRIVILEGE_HIERARCHY[hierarchy[server][db][SENTRY_PRIVILEGE_KEY]['action']]
          if table and table in hierarchy[server][db]:  # Get table-level privilege
            if SENTRY_PRIVILEGE_KEY in hierarchy[server][db][table]:
              privileges_applied['TABLE'] = PRIVILEGE_HIERARCHY[hierarchy[server][db][table][SENTRY_PRIVILEGE_KEY]['action']]
            if column and column in hierarchy[server][db][table]:  # Get column-level privilege
              # Since column is the lowest level, it must have a SENTRY_PRIVILEGE set
              privileges_applied['COLUMN'] = PRIVILEGE_HIERARCHY[hierarchy[server][db][table][column][SENTRY_PRIVILEGE_KEY]['action']]

    # A privilege hierarchy exists and at least one of the granted privileges is greater than or equal to requested action
    is_authorized = privileges_applied and max(privileges_applied.values()) >= requested_action_level
    return is_authorized


  def _is_object_action_authorized_v2(self, hierarchy, object, action='READ'):
    requested_action_level = PRIVILEGE_HIERARCHY[action]

    # Initialize all privileges for all object levels to non-authorized by default
    privileges_applied = dict((obj, -1) for obj in SENTRY_OBJECTS)

    component, service, obj_type, obj_name = object.get('component'), object.get('serviceName'), object.get('type'), object.get('name')

    if component and component in hierarchy:
      if service and service in hierarchy[component]:
        if obj_type and obj_type in hierarchy[component][service]:
          if obj_name and obj_name in hierarchy[component][service][obj_type]:
            if SENTRY_PRIVILEGE_KEY in hierarchy[component][service][obj_type][obj_name]:
              privileges_applied[obj_type] = PRIVILEGE_HIERARCHY[hierarchy[component][service][obj_type][obj_name][SENTRY_PRIVILEGE_KEY]['action']]

    # A privilege hierarchy exists and at least one of the granted privileges is greater than or equal to requested action
    is_authorized = privileges_applied and max(privileges_applied.values()) >= requested_action_level
    return is_authorized