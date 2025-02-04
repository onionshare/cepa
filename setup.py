#!/usr/bin/env python
# Copyright 2012-2019, Damian Johnson and The Tor Project
# See LICENSE for licensing information
#
# Release Checklist
# =================
#
# * Recache latest information (cache_manual.py and cache_fallback_directories.py)
#
# * Test with python2.6, python2.7, python3, and pypy.
#   |- If using tox run...
#   |
#   |    % tox -- --all --target RUN_ALL,ONLINE
#   |
#   |  Otherwise, for each interpreter run...
#   |
#   |    % [python_interpreter] run_tests.py --all --target RUN_ALL,ONLINE
#   |
#   |- Pypy test instructions for ubuntu are...
#   |
#   |    % sudo apt-get install pypy
#   |    % wget https://bootstrap.pypa.io/get-pip.py
#   |    % pypy get-pip.py --user
#   |    % ~/.local/bin/pip install mock pycodestyle pyflakes --user
#   |    % pypy ./run_tests.py --all
#   |
#   +- Some version of python 3.x should be available in your platform's
#      repositories. To test against a specific version on ubuntu try the
#      following. In this example, Python 3.7...
#
#        % sudo apt-get install build-essential python-dev python-setuptools python-pip python-smbus
#        % sudo apt-get install libncursesw5-dev libgdbm-dev libc6-dev
#        % sudo apt-get install zlib1g-dev libsqlite3-dev tk-dev
#        % sudo apt-get install libssl-dev openssl libffi-dev
#
#        % wget https://www.python.org/ftp/python/3.7.0/Python-3.7.0.tgz
#        % tar -xzf Python-3.7.0.tgz
#        % mv Python-3.7.0 ~
#
#        % cd ~/Python-3.7.0
#        % ./configure
#        % make
#
#        % cd /path/to/stem
#        % ~/Python-3.7.0/python ./run_tests.py --all
#
# * Tag the release
#   |- Bump stem's version (in stem/__init__.py and docs/index.rst).
#   |- git commit -a -m "Stem release 1.0.0"
#   |- git tag -u 9ABBEEC6 -m "stem release 1.0.0" 1.0.0 d0bb81a
#   +- git push --tags
#
# * Dry-run release on https://pypi.org/project/stem/
#   |- python setup.py sdist --dryrun
#   |- gpg --detach-sig --armor dist/stem-dry-run-1.0.0.tar.gz
#   |- twine upload dist/*
#   +- Check that https://pypi.org/project/stem-dry-run/ looks correct, comparing it to https://pypi.org/project/stem/
#      +- Don't worry about the 'Bug Tracker' being missing. That's an attribute of the project itself.
#
# * Final release
#   |- rm dist/*
#   |- python setup.py sdist
#   |- gpg --detach-sig --armor dist/stem-1.0.0.tar.gz
#   +- twine upload dist/*
#
# * Contact package maintainers
# * Announce the release (example: https://blog.torproject.org/blog/stem-release-11)

from distutils.core import setup
import os
import sys
import stem

if '--dryrun' in sys.argv:
  DRY_RUN = True
  sys.argv.remove('--dryrun')
else:
  DRY_RUN = False

SUMMARY = 'Stem is a Python controller library that allows applications to interact with Tor (https://www.torproject.org/).'
DRY_RUN_SUMMARY = 'Ignore this package. This is dry-run release creation to work around PyPI limitations (https://github.com/pypa/packaging-problems/issues/74#issuecomment-260716129).'

DESCRIPTION = """
For tutorials and API documentation see `Stem's homepage <https://stem.torproject.org/>`_.

Quick Start
-----------

To install you can either use...

::

  pip install stem

... or install from the source tarball. Stem supports both the python 2.x and 3.x series. To use its python3 counterpart you simply need to install using that version of python.

::

  python3 setup.py install

After that, give some `tutorials <https://stem.torproject.org/tutorials.html>`_ a try! For questions or to discuss project ideas we're available on `irc <https://www.torproject.org/about/contact.html.en#irc>`_ and the `tor-dev@ email list <https://lists.torproject.org/cgi-bin/mailman/listinfo/tor-dev>`_.
""".strip()

MANIFEST = """
include cache_fallback_directories.py
include cache_manual.py
include LICENSE
include README.md
include MANIFEST.in
include requirements.txt
include run_tests.py
include tox.ini
graft docs
graft test
global-exclude __pycache__
global-exclude *.orig
global-exclude *.pyc
global-exclude *.swp
global-exclude *.swo
global-exclude .tox
global-exclude *~
recursive-exclude test/data *
recursive-exclude docs/_build *
""".strip()

# installation requires us to be in our setup.py's directory

os.chdir(os.path.dirname(os.path.abspath(__file__)))

with open('MANIFEST.in', 'w') as manifest_file:
  manifest_file.write(MANIFEST)

setup(
  name = 'cepa',
  version = '1.8.4',
  description = DRY_RUN_SUMMARY if DRY_RUN else SUMMARY,
  long_description = DESCRIPTION,
  license = stem.__license__,
  author = stem.__author__,
  author_email = stem.__contact__,
  url = stem.__url__,
  packages = ['stem', 'stem.client', 'stem.descriptor', 'stem.interpreter', 'stem.response', 'stem.util'],
  keywords = 'tor onion controller',
  scripts = ['tor-prompt'],
  package_data = {
    'stem': ['cached_fallbacks.cfg', 'cached_manual.sqlite', 'settings.cfg'],
    'stem.interpreter': ['settings.cfg'],
    'stem.util': ['ports.cfg'],
  }, classifiers = [
    'Development Status :: 5 - Production/Stable',
    'Intended Audience :: Developers',
    'License :: OSI Approved :: GNU Lesser General Public License v3 (LGPLv3)',
    'Topic :: Security',
    'Topic :: Software Development :: Libraries :: Python Modules',
  ],
  install_requires=[
    "setuptools >= 65.4.1"
  ]
)

if os.path.exists('MANIFEST.in'):
  os.remove('MANIFEST.in')

if os.path.exists('MANIFEST'):
  os.remove('MANIFEST')
