#!/usr/bin/env python3

# Copyright 2019 Open Source Robotics Foundation, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
from collections import defaultdict
import copy
import json
import multiprocessing.pool
import os
import re
import subprocess
import sys
import time

from xml.sax.saxutils import quoteattr

import yaml


def main(argv=sys.argv[1:]):
    config_file = os.path.join(
        os.path.dirname(__file__), 'configuration', '.clang-tidy')
    extensions = ['c', 'cc', 'cpp', 'cxx', 'h', 'hh', 'hpp', 'hxx']

    parser = argparse.ArgumentParser(
        description='Check code style using clang_tidy.',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '--config',
        metavar='path',
        default=config_file,
        dest='config_file',
        help='The config file')
    parser.add_argument(
        'paths',
        nargs='*',
        default=[os.curdir],
        help='If <path> is a directory, ament_clang_tidy will recursively search it for'
             ' "compile_commands.json" files. If <path> is a file, ament_clang_tidy will'
             ' treat it as a "compile_commands.json" file')
    parser.add_argument(
        '--jobs',
        type=int,
        default=1,
        help='number of clang-tidy jobs to run in parallel')

    # not using a file handle directly
    # in order to prevent leaving an empty file when something fails early
    parser.add_argument(
        '--explain-config',
        action='store_true',
        help='Explain the enabled checks')
    parser.add_argument(
        '--export-fixes',
        help='Generate a DAT file of recorded fixes')
    parser.add_argument(
        '--quiet',
        action='store_true',
        help='Suppresses printing statistics about ignored warnings '
             'and warnings treated as errors')
    parser.add_argument(
        '--system-headers',
        action='store_true',
        help='Displays errors from all system headers')
    parser.add_argument(
        '--xunit-file',
        help='Generate a xunit compliant XML file')
    args = parser.parse_args(argv)

    if not os.path.exists(args.config_file):
        print("Could not find config file '%s'" % args.config_file, file=sys.stderr)
        return 1

    if args.xunit_file:
        start_time = time.time()

    files = get_compilation_db_files(args.paths)
    if not files:
        print('No compilation database files found', file=sys.stderr)
        return 1

    bin_names = [
        'clang-tidy',
        'clang-tidy-6.0',
    ]
    clang_tidy_bin = find_executable(bin_names)
    if not clang_tidy_bin:
        print('Could not find %s executable' %
              ' / '.join(["'%s'" % n for n in bin_names]), file=sys.stderr)
        return 1

    pool = multiprocessing.pool.ThreadPool(args.jobs)
    async_outputs = []
    for file in files:
        package_dir = os.path.dirname(file)
        package_name = os.path.basename(package_dir)
        print('linting ' + package_name + '...')
        async_outputs.append(pool.apply_async(invoke_clang_tidy, (clang_tidy_bin, file, args)))
    pool.close()
    pool.join()

    error_re = re.compile('(/.*\\.(?:%s)):(\\d+):(\\d+):' % '|'.join(extensions))

    # output errors
    report = defaultdict(list)
    current_file = None
    new_file = None
    data = {}

    for async_output in async_outputs:
        output = async_output.get()
        for line in output.splitlines():
            # error found
            match = error_re.search(line)
            if match:
                new_file = match.group(1)
                if current_file is not None:
                    report[current_file].append(copy.deepcopy(data))
                    data.clear()
                current_file = new_file
                line_num = match.group(2)
                col_num = match.group(3)
                error_msg = find_error_message(line)
                data['line_no'] = line_num
                data['offset_in_line'] = col_num
                data['error_msg'] = error_msg
            else:
                data['code_correct_rec'] = data.get('code_correct_rec', '') + line + '\n'
        if current_file is not None:
            report[current_file].append(copy.deepcopy(data))

    if args.xunit_file:
        folder_name = os.path.basename(os.path.dirname(args.xunit_file))
        file_name = os.path.basename(args.xunit_file)
        suffix = '.xml'
        if file_name.endswith(suffix):
            file_name = file_name[0:-len(suffix)]
            suffix = '.xunit'
            if file_name.endswith(suffix):
                file_name = file_name[0:-len(suffix)]
        testname = '%s.%s' % (folder_name, file_name)
        xml = get_xunit_content(report, testname, time.time() - start_time)
        path = os.path.dirname(os.path.abspath(args.xunit_file))
        if not os.path.exists(path):
            os.makedirs(path)
        with open(args.xunit_file, 'w') as f:
            f.write(xml)


def find_executable(file_names):
    paths = os.getenv('PATH').split(os.path.pathsep)
    for file_name in file_names:
        for path in paths:
            file_path = os.path.join(path, file_name)
            if os.path.isfile(file_path) and os.access(file_path, os.X_OK):
                return file_path
    return None


def get_compilation_db_files(paths):
    files = []
    for path in paths:
        if os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                if 'AMENT_IGNORE' in filenames:
                    dirnames[:] = []
                    continue
                # ignore folder starting with . or _
                dirnames[:] = [d for d in dirnames if d[0] not in ['.', '_']]
                dirnames.sort()

                # select files by extension
                for filename in filenames:
                    if filename == 'compile_commands.json':
                        files.append(os.path.join(dirpath, filename))
        elif os.path.isfile(path):
            files.append(path)
    return [os.path.normpath(f) for f in files]


def invoke_clang_tidy(clang_tidy_bin, compilation_db_path, args):
    package_dir = os.path.dirname(compilation_db_path)
    package_name = os.path.basename(package_dir)

    with open(args.config_file, 'r') as h:
        content = h.read()
    data = yaml.safe_load(content)
    style = yaml.dump(data, default_flow_style=True, width=float('inf'))
    cmd = [clang_tidy_bin, '--config=%s' % style, '--header-filter',
           'include/%s/.*' % package_name, '-p', package_dir]
    if args.explain_config:
        cmd.append('--explain-config')
    if args.export_fixes:
        cmd.append('--export-fixes')
        cmd.append(args.export_fixes)
    if args.quiet:
        cmd.append('--quiet')
    if args.system_headers:
        cmd.append('--system-headers')

    def is_gtest_source(file_name):
        if(file_name == 'gtest_main.cc' or file_name == 'gtest-all.cc'
           or file_name == 'gmock_main.cc' or file_name == 'gmock-all.cc'):
            return True
        return False

    def is_unittest_source(package, file_path):
        return ('%s/test/' % package) in file_path

    output = ''
    db = json.load(open(compilation_db_path))
    for item in db:
        # exclude gtest sources from being checked by clang-tidy
        if is_gtest_source(os.path.basename(item['file'])):
            continue
        # exclude unit test sources from being checked by clang-tidy
        # because gtest macros are problematic
        if is_unittest_source(package_name, item['file']):
            continue

        full_cmd = cmd + [item['file']]
        # print(' '.join(full_cmd))
        try:
            output += subprocess.check_output(full_cmd).strip().decode()
        except subprocess.CalledProcessError as e:
            print('The invocation of "%s" failed with error code %d: %s' %
                  (os.path.basename(clang_tidy_bin), e.returncode, e),
                  file=sys.stderr)
    return output


def find_error_message(data):
    return data[data.rfind(':') + 2:]


def get_xunit_content(report, testname, elapsed):
    test_count = sum(max(len(r), 1) for r in report.values())
    error_count = sum(len(r) for r in report.values())
    data = {
        'testname': testname,
        'test_count': test_count,
        'error_count': error_count,
        'time': '%.3f' % round(elapsed, 3),
    }
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<testsuite
  name="%(testname)s"
  tests="%(test_count)d"
  failures="%(error_count)d"
  time="%(time)s"
>
""" % data

    for filename in sorted(report.keys()):
        errors = report[filename]

        if errors:
            # report each replacement as a failing testcase
            for error in errors:
                data = {
                    'quoted_location': quoteattr(
                        '%s:%d:%d' % (
                            filename, int(error['line_no']),
                            int(error['offset_in_line']))),
                    'testname': testname,
                    'quoted_message': quoteattr(
                        '%s' %
                        error['error_msg']),
                    'cdata': '\n'.join([
                        '%s:%d:%d' % (
                            filename, int(error['line_no']),
                            int(error['offset_in_line']))])
                }
                if 'code_correct_rec' in data:
                    data['cdata'] += '\n'
                    data['cdata'] += data['code_correct_rec']
                xml += """  <testcase
    name=%(quoted_location)s
    classname="%(testname)s"
  >
      <failure message=%(quoted_message)s><![CDATA[%(cdata)s]]></failure>
  </testcase>
""" % data

        else:
            # if there are no errors report a single successful test
            data = {
                'quoted_location': quoteattr(filename),
                'testname': testname,
            }
            xml += """  <testcase
    name=%(quoted_location)s
    classname="%(testname)s"
    status="No problems found"/>
""" % data

    xml += '</testsuite>\n'
    return xml


if __name__ == '__main__':
    sys.exit(main())
