# Copyright 2015 Open Source Robotics Foundation, Inc.
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

import os
import re

from ament_copyright import ALL_FILETYPES
from ament_copyright import CONTRIBUTING_FILETYPE
from ament_copyright import get_copyright_names
from ament_copyright import get_licenses
from ament_copyright import LICENSE_FILETYPE
from ament_copyright import SOURCE_FILETYPE
from ament_copyright import UNKNOWN_IDENTIFIER


class FileDescriptor(object):

    def __init__(self, filetype, path):
        self.filetype = filetype
        self.path = path
        self.exists = os.path.exists(path)
        self.content = None

    def read(self):
        if not self.exists:
            return
        with open(self.path, 'r') as h:
            self.content = h.read()

    def parse(self):
        raise NotImplemented()

    def identify_copyright(self):
        for identifier, name in get_copyright_names().items():
            if self.copyright_name is not None and self.copyright_name == name:
                self.copyright_identifier = identifier
                break
        else:
            self.copyright_identifier = UNKNOWN_IDENTIFIER

    def identify_license(self, content, license_part):
        for name, license in get_licenses().items():
            if content is not None and getattr(license, license_part) == content:
                self.license_identifier = name
                break
        else:
            self.license_identifier = UNKNOWN_IDENTIFIER


class SourceDescriptor(FileDescriptor):

    def __init__(self, path):
        super(SourceDescriptor, self).__init__(SOURCE_FILETYPE, path)

        self.copyright_name = None
        self.copyright_years = None

        self.copyright_identifier = None
        self.license_identifier = None

    def parse(self):
        self.read()
        if not self.content:
            return

        # skip over  coding and shebang lines
        index = scan_past_coding_and_shebang_lines(self.content)
        index = scan_past_empty_lines(self.content, index)

        # get first comment block without leading comment tokens
        block, _ = get_comment_block(self.content, index)
        if not block:
            return
        copyright_span, years_span, name_span = _search_copyright_information(block)
        if copyright_span is None:
            return None

        self.copyright_years = block[years_span[0]:years_span[1]]
        self.copyright_name = block[name_span[0]:name_span[1]]

        self.identify_copyright()

        content = '{copyright}' + block[name_span[1]:]
        self.identify_license(content, 'file_header')


class ContributingDescriptor(FileDescriptor):

    def __init__(self, path):
        super(ContributingDescriptor, self).__init__(CONTRIBUTING_FILETYPE, path)

        self.license_identifier = None

    def parse(self):
        self.read()
        if not self.content:
            return

        self.identify_license(self.content, 'contributing_file')


class LicenseDescriptor(FileDescriptor):

    def __init__(self, path):
        super(LicenseDescriptor, self).__init__(LICENSE_FILETYPE, path)

        self.copyright_years = None
        self.copyright_name = None

        self.copyright_identifier = None
        self.license_identifier = None

    def parse(self):
        self.read()
        if not self.content:
            return

        self.identify_copyright()

        content = _replace_copyright_with_placeholder(self.content, self)
        self.identify_license(content, 'license_file')


def parse_file(path):
    filetype = determine_filetype(path)
    if filetype == SOURCE_FILETYPE:
        d = SourceDescriptor(path)
    elif filetype == CONTRIBUTING_FILETYPE:
        d = ContributingDescriptor(path)
    elif filetype == LICENSE_FILETYPE:
        d = LicenseDescriptor(path)
    else:
        return None
    d.parse()
    return d


def determine_filetype(path):
    basename = os.path.basename(path)
    for filetype, filename in ALL_FILETYPES.items():
        if basename == filename:
            return filetype
    return SOURCE_FILETYPE


def _replace_copyright_with_placeholder(content, file_descriptor):
    copyright_span, years_span, name_span = _search_copyright_information(content)
    if copyright_span is None:
        return None

    file_descriptor.copyright_years = content[years_span[0]:years_span[1]]
    file_descriptor.copyright_name = content[name_span[0]:name_span[1]]

    return content[:copyright_span[0]] + '{copyright}' + content[name_span[1]:]


def _search_copyright_information(content):
    # regex for matching years or year ranges (yyyy-yyyy) separated by colons
    year = '\d{4}'
    year_range = '%s-%s' % (year, year)
    year_or_year_range = '(?:%s|%s)' % (year, year_range)
    pattern = '^[^\n\r]?\s*(Copyright)\s+(%s(?:,\s*%s)*)\s+([^\n\r]+)$' % \
        (year_or_year_range, year_or_year_range)
    regex = re.compile(pattern, re.DOTALL | re.MULTILINE)

    match = regex.search(content)
    if not match:
        return None, None, None
    return match.span(1), match.span(2), match.span(3)


def scan_past_coding_and_shebang_lines(content):
    index = 0
    while (
        is_comment_line(content, index) and
        (is_coding_line(content, index) or
         is_shebang_line(content, index))
    ):
        index = get_index_of_next_line(content, index)
    return index


def get_index_of_next_line(content, index):
    index_n = content.find('\n', index)
    index_r = content.find('\r', index)
    index_rn = content.find('\r\n', index)
    indices = set([])
    if index_n != -1:
        indices.add(index_n)
    if index_r != -1:
        indices.add(index_r)
    if index_rn != -1:
        indices.add(index_rn)
    if not indices:
        return len(content)
    index = min(indices)
    if index == index_rn:
        return index + 2
    return index + 1


def is_comment_line(content, index):
    return content[index] == '#' or content[index:index + 1] == '//'


def is_coding_line(content, index):
    end_index = get_index_of_next_line(content, index)
    line = content[index:end_index]
    return 'coding=' in line or 'coding:' in line


def is_shebang_line(content, index):
    return content[index:index + 2] == '#!'


def get_comment_block(content, index):
    # regex for matching the beginning of the first comment
    pattern = '^(#|//)'
    regex = re.compile(pattern, re.MULTILINE)

    match = regex.search(content, index)
    if not match:
        return None, None
    comment_token = match.group(1)
    start_index = match.start(1)

    end_index = start_index
    while True:
        end_index = get_index_of_next_line(content, end_index)
        if content[end_index:end_index + len(comment_token)] != comment_token:
            break

    block = content[start_index:end_index]
    lines = block.splitlines()
    lines = [line[len(comment_token) + 1:] for line in lines]

    return '\n'.join(lines), start_index + len(comment_token) + 1


def scan_past_empty_lines(content, index):
    while is_empty_line(content, index):
        index = get_index_of_next_line(content, index)
    return index


def is_empty_line(content, index):
    return get_index_of_next_line(content, index) == index + 1
