# Copyright 2015-2019, Damian Johnson and The Tor Project
# See LICENSE for licensing information

"""
Parsing for Tor hidden service descriptors as described in Tor's `version 2
<https://gitweb.torproject.org/torspec.git/tree/rend-spec-v2.txt>`_ and
`version 3 <https://gitweb.torproject.org/torspec.git/tree/rend-spec-v3.txt>`_
rend-spec.

Unlike other descriptor types these describe a hidden service rather than a
relay. They're created by the service, and can only be fetched via relays with
the HSDir flag.

These are only available through the Controller's
:func:`~stem.control.Controller.get_hidden_service_descriptor` method.

**Module Overview:**

::

  BaseHiddenServiceDescriptor - Common parent for hidden service descriptors
    |- HiddenServiceDescriptorV2 - Version 2 hidden service descriptor
    +- HiddenServiceDescriptorV3 - Version 3 hidden service descriptor
         +- decrypt - decrypt and parse encrypted layers

  OuterLayer - First encrypted layer of a hidden service v3 descriptor
  InnerLayer - Second encrypted layer of a hidden service v3 descriptor

.. versionadded:: 1.4.0
"""

import base64
import binascii
import collections
import datetime
import hashlib
import io
import os
import struct
import time

import stem.client.datatype
import stem.descriptor.certificate
import stem.descriptor.hsv3_crypto
import stem.prereq
import stem.util.connection
import stem.util.str_tools
import stem.util.tor_tools

from stem.client.datatype import CertType
from stem.descriptor.certificate import ExtensionType, Ed25519Extension, Ed25519Certificate, Ed25519CertificateV1

from stem.descriptor import (
  PGP_BLOCK_END,
  Descriptor,
  _descriptor_content,
  _descriptor_components,
  _read_until_keywords,
  _bytes_for_block,
  _value,
  _values,
  _parse_simple_line,
  _parse_if_present,
  _parse_int_line,
  _parse_timestamp_line,
  _parse_key_block,
  _random_date,
  _random_crypto_blob,
)

if stem.prereq._is_lru_cache_available():
  from functools import lru_cache
else:
  from stem.util.lru_cache import lru_cache

try:
  from cryptography.hazmat.backends.openssl.backend import backend
  X25519_AVAILABLE = backend.x25519_supported()
except ImportError:
  X25519_AVAILABLE = False


REQUIRED_V2_FIELDS = (
  'rendezvous-service-descriptor',
  'version',
  'permanent-key',
  'secret-id-part',
  'publication-time',
  'protocol-versions',
  'signature',
)

REQUIRED_V3_FIELDS = (
  'hs-descriptor',
  'descriptor-lifetime',
  'descriptor-signing-key-cert',
  'revision-counter',
  'superencrypted',
  'signature',
)

INTRODUCTION_POINTS_ATTR = {
  'identifier': None,
  'address': None,
  'port': None,
  'onion_key': None,
  'service_key': None,
  'intro_authentication': [],
}

# introduction-point fields that can only appear once

SINGLE_INTRODUCTION_POINT_FIELDS = [
  'introduction-point',
  'ip-address',
  'onion-port',
  'onion-key',
  'service-key',
]

BASIC_AUTH = 1
STEALTH_AUTH = 2
CHECKSUM_CONSTANT = b'.onion checksum'

SALT_LEN = 16
MAC_LEN = 32

S_KEY_LEN = 32
S_IV_LEN = 16


class DecryptionFailure(Exception):
  """
  Failure to decrypt the hidden service descriptor's introduction-points.
  """


# TODO: rename in stem 2.x (add 'V2' and drop plural)

class IntroductionPoints(collections.namedtuple('IntroductionPoints', INTRODUCTION_POINTS_ATTR.keys())):
  """
  Introduction point for a v2 hidden service.

  :var str identifier: hash of this introduction point's identity key
  :var str address: address of this introduction point
  :var int port: port where this introduction point is listening
  :var str onion_key: public key for communicating with this introduction point
  :var str service_key: public key for communicating with this hidden service
  :var list intro_authentication: tuples of the form (auth_type, auth_data) for
    establishing a connection
  """


class IntroductionPointV3(collections.namedtuple('IntroductionPointV3', ['link_specifiers', 'onion_key_raw', 'auth_key_cert', 'enc_key_raw', 'enc_key_cert', 'legacy_key_raw', 'legacy_key_cert'])):
  """
  Introduction point for a v3 hidden service.

  .. versionadded:: 1.8.0

  :var list link_specifiers: :class:`~stem.client.datatype.LinkSpecifier` where this service is reachable
  :var str onion_key_raw: base64 ntor introduction point public key
  :var stem.descriptor.certificate.Ed25519Certificate auth_key_cert: cross-certifier of the signing key with the auth key
  :var str enc_key_raw: base64 introduction request encryption key
  :var stem.descriptor.certificate.Ed25519Certificate enc_key_cert: cross-certifier of the signing key by the encryption key
  :var str legacy_key_raw: base64 legacy introduction point RSA public key
  :var str legacy_key_cert: base64 cross-certifier of the signing key by the legacy key
  """

  @staticmethod
  def parse(content):
    """
    Parses an introduction point from its descriptor content.

    :param str content: descriptor content to parse

    :returns: :class:`~stem.descriptor.hidden_service.IntroductionPointV3` for the descriptor content

    :raises: **ValueError** if descriptor content is malformed
    """

    entry = _descriptor_components(content, False)
    link_specifiers = IntroductionPointV3._parse_link_specifiers(_value('introduction-point', entry))

    onion_key_line = _value('onion-key', entry)
    onion_key = onion_key_line[5:] if onion_key_line.startswith('ntor ') else None

    _, block_type, auth_key_cert = entry['auth-key'][0]
    auth_key_cert = Ed25519Certificate.from_base64(auth_key_cert)

    if block_type != 'ED25519 CERT':
      raise ValueError('Expected auth-key to have an ed25519 certificate, but was %s' % block_type)

    enc_key_line = _value('enc-key', entry)
    enc_key = enc_key_line[5:] if enc_key_line.startswith('ntor ') else None

    _, block_type, enc_key_cert = entry['enc-key-cert'][0]
    enc_key_cert = Ed25519Certificate.from_base64(enc_key_cert)

    if block_type != 'ED25519 CERT':
      raise ValueError('Expected enc-key-cert to have an ed25519 certificate, but was %s' % block_type)

    legacy_key = entry['legacy-key'][0][2] if 'legacy-key' in entry else None
    legacy_key_cert = entry['legacy-key-cert'][0][2] if 'legacy-key-cert' in entry else None

    return IntroductionPointV3(link_specifiers, onion_key, auth_key_cert, enc_key, enc_key_cert, legacy_key, legacy_key_cert)

  @staticmethod
  def create(address, port, expiration, onion_key, enc_key, auth_key, signing_key):
    """
    Simplified constructor. For more sophisticated use cases you can use this
    as a template for how introduction points are properly created.

    :param str address: IPv4 or IPv6 address where the service is reachable
    :param int port: port where the service is reachable
    :param datetime.datetime expiration: when certificates should expire
    :param str onion_key: encoded, X25519PublicKey, or X25519PrivateKey onion key
    :param str enc_key: encoded, X25519PublicKey, or X25519PrivateKey encryption key
    :param str auth_key: encoded, Ed25519PublicKey, or Ed25519PrivateKey authentication key
    :param cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PrivateKey signing_key: service signing key

    :returns: :class:`~stem.descriptor.hidden_service.IntroductionPointV3` with these attributes

    :raises: **ValueError** if the address, port, or keys are malformed
    """

    def _key_bytes(key):
      class_name = type(key).__name__

      if isinstance(key, str):
        return key
      elif not class_name.endswith('PublicKey') and not class_name.endswith('PrivateKey'):
        raise ValueError('Key must be a string or cryptographic public/private key (was %s)' % class_name)
      elif not stem.prereq.is_crypto_available():
        raise ImportError('Serializing keys requires the cryptography module')

      from cryptography.hazmat.primitives import serialization

      if class_name.endswith('PrivateKey'):
        key = key.public_key()

      return key.public_bytes(
        encoding = serialization.Encoding.Raw,
        format = serialization.PublicFormat.Raw,
      )

    if not stem.util.connection.is_valid_port(port):
      raise ValueError("'%s' is an invalid port" % port)

    if stem.util.connection.is_valid_ipv4_address(address):
      link_specifiers = [stem.client.datatype.LinkByIPv4(address, port)]
    elif stem.util.connection.is_valid_ipv6_address(address):
      link_specifiers = [stem.client.datatype.LinkByIPv6(address, port)]
    else:
      raise ValueError("'%s' is not a valid IPv4 or IPv6 address" % address)

    onion_key = base64.b64encode(_key_bytes(onion_key))
    enc_key = base64.b64encode(_key_bytes(enc_key))

    extensions = [Ed25519Extension(ExtensionType.HAS_SIGNING_KEY, None, _key_bytes(signing_key))]
    auth_key_cert = Ed25519CertificateV1(CertType.HS_V3_INTRO_AUTH, expiration, 1, _key_bytes(auth_key), extensions, signing_key = signing_key)
    enc_key_cert = Ed25519CertificateV1(CertType.HS_V3_NTOR_ENC, expiration, 1, _key_bytes(auth_key), extensions, signing_key = signing_key)

    return IntroductionPointV3(link_specifiers, onion_key, auth_key_cert, enc_key, enc_key_cert, None, None)

  def encode(self):
    """
    Descriptor representation of this introduction point.

    :returns: **str** for our descriptor representation
    """

    lines = []

    link_count = stem.client.datatype.Size.CHAR.pack(len(self.link_specifiers))
    link_specifiers = link_count + b''.join([l.pack() for l in self.link_specifiers])
    lines.append('introduction-point %s' % stem.util.str_tools._to_unicode(base64.b64encode(link_specifiers)))

    if self.onion_key_raw:
      lines.append('onion-key ntor %s' % self.onion_key_raw)

    lines.append('auth-key\n' + self.auth_key_cert.to_base64(pem = True))

    if self.enc_key_raw:
      lines.append('enc-key ntor %s' % self.enc_key_raw)

    lines.append('enc-key-cert\n' + self.enc_key_cert.to_base64(pem = True))

    if self.legacy_key_raw:
      lines.append('legacy-key\n' + self.legacy_key_raw)

    if self.legacy_key_cert:
      lines.append('legacy-key-cert\n' + self.legacy_key_cert)

    return '\n'.join(lines)

  def onion_key(self):
    """
    Provides our ntor introduction point public key.

    :returns: ntor :class:`~cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey`

    :raises:
      * **ImportError** if required the cryptography module is unavailable
      * **EnvironmentError** if OpenSSL x25519 unsupported
    """

    return IntroductionPointV3._key_as(self.onion_key_raw, x25519 = True)

  def auth_key(self):
    """
    Provides our authentication certificate's public key.

    :returns: :class:`~cryptography.hazmat.primitives.asymmetric.ed25519.Ed25519PublicKey`

    :raises:
      * **ImportError** if required the cryptography module is unavailable
      * **EnvironmentError** if OpenSSL x25519 unsupported
    """

    return IntroductionPointV3._key_as(self.auth_key_cert.key, ed25519 = True)

  def enc_key(self):
    """
    Provides our encryption key.

    :returns: encryption :class:`~cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey`

    :raises:
      * **ImportError** if required the cryptography module is unavailable
      * **EnvironmentError** if OpenSSL x25519 unsupported
    """

    return IntroductionPointV3._key_as(self.enc_key_raw, x25519 = True)

  def legacy_key(self):
    """
    Provides our legacy introduction point public key.

    :returns: legacy :class:`~cryptography.hazmat.primitives.asymmetric.x25519.X25519PublicKey`

    :raises:
      * **ImportError** if required the cryptography module is unavailable
      * **EnvironmentError** if OpenSSL x25519 unsupported
    """

    return IntroductionPointV3._key_as(self.legacy_key_raw, x25519 = True)

  @staticmethod
  def _key_as(value, x25519 = False, ed25519 = False):
    if value is None or (not x25519 and not ed25519):
      return value
    elif not stem.prereq.is_crypto_available():
      raise ImportError('cryptography module unavailable')

    if x25519:
      if not X25519_AVAILABLE:
        # without this the cryptography raises...
        # cryptography.exceptions.UnsupportedAlgorithm: X25519 is not supported by this version of OpenSSL.

        raise EnvironmentError('OpenSSL x25519 unsupported')

      from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey
      return X25519PublicKey.from_public_bytes(base64.b64decode(value))

    if ed25519:
      if not stem.prereq.is_crypto_available(ed25519 = True):
        raise EnvironmentError('cryptography ed25519 unsupported')

      from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
      return Ed25519PublicKey.from_public_bytes(value)

  @staticmethod
  def _parse_link_specifiers(content):
    try:
      content = base64.b64decode(content)
    except Exception as exc:
      raise ValueError('Unable to base64 decode introduction point (%s): %s' % (exc, content))

    link_specifiers = []
    count, content = stem.client.datatype.Size.CHAR.pop(content)

    for i in range(count):
      link_specifier, content = stem.client.datatype.LinkSpecifier.pop(content)
      link_specifiers.append(link_specifier)

    if content:
      raise ValueError('Introduction point had excessive data (%s)' % content)

    return link_specifiers


class AuthorizedClient(collections.namedtuple('AuthorizedClient', ['id', 'iv', 'cookie'])):
  """
  Client authorized to use a v3 hidden service.

  .. versionadded:: 1.8.0

  :var str id: base64 encoded client id
  :var str iv: base64 encoded randomized initialization vector
  :var str cookie: base64 encoded authentication cookie
  """


def _parse_file(descriptor_file, desc_type = None, validate = False, **kwargs):
  """
  Iterates over the hidden service descriptors in a file.

  :param file descriptor_file: file with descriptor content
  :param class desc_type: BaseHiddenServiceDescriptor subclass
  :param bool validate: checks the validity of the descriptor's content if
    **True**, skips these checks otherwise
  :param dict kwargs: additional arguments for the descriptor constructor

  :returns: iterator for :class:`~stem.descriptor.hidden_service.HiddenServiceDescriptorV2`
    instances in the file

  :raises:
    * **ValueError** if the contents is malformed and validate is **True**
    * **IOError** if the file can't be read
  """

  if desc_type is None:
    desc_type = HiddenServiceDescriptorV2

  # Hidden service v3 ends with a signature line, whereas v2 has a pgp style
  # block following it.

  while True:
    descriptor_content = _read_until_keywords('signature', descriptor_file, True)

    if desc_type == HiddenServiceDescriptorV2:
      block_end_prefix = PGP_BLOCK_END.split(' ', 1)[0]
      descriptor_content += _read_until_keywords(block_end_prefix, descriptor_file, True)

    if descriptor_content:
      if descriptor_content[0].startswith(b'@type'):
        descriptor_content = descriptor_content[1:]

      yield desc_type(bytes.join(b'', descriptor_content), validate, **kwargs)
    else:
      break  # done parsing file


def _decrypt_layer(encrypted_block, constant, revision_counter, subcredential, blinded_key):
  from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
  from cryptography.hazmat.backends import default_backend

  def pack(val):
    return struct.pack('>Q', val)

  if encrypted_block.startswith('-----BEGIN MESSAGE-----\n') and encrypted_block.endswith('\n-----END MESSAGE-----'):
    encrypted_block = encrypted_block[24:-22]

  try:
    encrypted = base64.b64decode(encrypted_block)
  except:
    raise ValueError('Unable to decode encrypted block as base64')

  if len(encrypted) < SALT_LEN + MAC_LEN:
    raise ValueError('Encrypted block malformed (only %i bytes)' % len(encrypted))

  salt = encrypted[:SALT_LEN]
  ciphertext = encrypted[SALT_LEN:-MAC_LEN]
  expected_mac = encrypted[-MAC_LEN:]

  kdf = hashlib.shake_256(blinded_key + subcredential + pack(revision_counter) + salt + constant)
  keys = kdf.digest(S_KEY_LEN + S_IV_LEN + MAC_LEN)

  secret_key = keys[:S_KEY_LEN]
  secret_iv = keys[S_KEY_LEN:S_KEY_LEN + S_IV_LEN]
  mac_key = keys[S_KEY_LEN + S_IV_LEN:]

  mac = hashlib.sha3_256(pack(len(mac_key)) + mac_key + pack(len(salt)) + salt + ciphertext).digest()

  if mac != expected_mac:
    raise ValueError('Malformed mac (expected %s, but was %s)' % (expected_mac, mac))

  cipher = Cipher(algorithms.AES(secret_key), modes.CTR(secret_iv), default_backend())
  decryptor = cipher.decryptor()

  return stem.util.str_tools._to_unicode(decryptor.update(ciphertext) + decryptor.finalize())


def _parse_protocol_versions_line(descriptor, entries):
  value = _value('protocol-versions', entries)

  try:
    versions = [int(entry) for entry in value.split(',')]
  except ValueError:
    raise ValueError('protocol-versions line has non-numeric versoins: protocol-versions %s' % value)

  for v in versions:
    if v <= 0:
      raise ValueError('protocol-versions must be positive integers: %s' % value)

  descriptor.protocol_versions = versions


def _parse_introduction_points_line(descriptor, entries):
  _, block_type, block_contents = entries['introduction-points'][0]

  if not block_contents or block_type != 'MESSAGE':
    raise ValueError("'introduction-points' should be followed by a MESSAGE block, but was a %s" % block_type)

  descriptor.introduction_points_encoded = block_contents
  descriptor.introduction_points_auth = []  # field was never implemented in tor (#15190)

  try:
    descriptor.introduction_points_content = _bytes_for_block(block_contents)
  except TypeError:
    raise ValueError("'introduction-points' isn't base64 encoded content:\n%s" % block_contents)


def _parse_v3_outer_clients(descriptor, entries):
  # "auth-client" client-id iv encrypted-cookie

  clients = {}

  for value in _values('auth-client', entries):
    value_comp = value.split()

    if len(value_comp) < 3:
      raise ValueError('auth-client should have a client-id, iv, and cookie: auth-client %s' % value)

    clients[value_comp[0]] = AuthorizedClient(value_comp[0], value_comp[1], value_comp[2])

  descriptor.clients = clients


def _parse_v3_inner_formats(descriptor, entries):
  value, formats = _value('create2-formats', entries), []

  for entry in value.split(' '):
    if not entry.isdigit():
      raise ValueError("create2-formats should only contain integers, but was '%s'" % value)

    formats.append(int(entry))

  descriptor.formats = formats


def _parse_v3_introduction_points(descriptor, entries):
  if hasattr(descriptor, '_unparsed_introduction_points'):
    introduction_points = []
    remaining = descriptor._unparsed_introduction_points

    while remaining:
      div = remaining.find('\nintroduction-point ', 10)
      content, remaining = (remaining[:div], remaining[div + 1:]) if div != -1 else (remaining, '')

      introduction_points.append(IntroductionPointV3.parse(content))

    descriptor.introduction_points = introduction_points
    del descriptor._unparsed_introduction_points


_parse_v2_version_line = _parse_int_line('version', 'version', allow_negative = False)
_parse_rendezvous_service_descriptor_line = _parse_simple_line('rendezvous-service-descriptor', 'descriptor_id')
_parse_permanent_key_line = _parse_key_block('permanent-key', 'permanent_key', 'RSA PUBLIC KEY')
_parse_secret_id_part_line = _parse_simple_line('secret-id-part', 'secret_id_part')
_parse_publication_time_line = _parse_timestamp_line('publication-time', 'published')
_parse_v2_signature_line = _parse_key_block('signature', 'signature', 'SIGNATURE')

_parse_v3_version_line = _parse_int_line('hs-descriptor', 'version', allow_negative = False)
_parse_lifetime_line = _parse_int_line('descriptor-lifetime', 'lifetime', allow_negative = False)
_parse_signing_cert = Ed25519Certificate._from_descriptor('descriptor-signing-key-cert', 'signing_cert')
_parse_revision_counter_line = _parse_int_line('revision-counter', 'revision_counter', allow_negative = False)
_parse_superencrypted_line = _parse_key_block('superencrypted', 'superencrypted', 'MESSAGE')
_parse_v3_signature_line = _parse_simple_line('signature', 'signature')

_parse_v3_outer_auth_type = _parse_simple_line('desc-auth-type', 'auth_type')
_parse_v3_outer_ephemeral_key = _parse_simple_line('desc-auth-ephemeral-key', 'ephemeral_key')
_parse_v3_outer_encrypted = _parse_key_block('encrypted', 'encrypted', 'MESSAGE')

_parse_v3_inner_intro_auth = _parse_simple_line('intro-auth-required', 'intro_auth', func = lambda v: v.split(' '))
_parse_v3_inner_single_service = _parse_if_present('single-onion-service', 'is_single_service')


class BaseHiddenServiceDescriptor(Descriptor):
  """
  Hidden service descriptor.

  .. versionadded:: 1.8.0
  """

  # TODO: rename this class to HiddenServiceDescriptor in stem 2.x


class HiddenServiceDescriptorV2(BaseHiddenServiceDescriptor):
  """
  Version 2 hidden service descriptor.

  :var str descriptor_id: **\\*** identifier for this descriptor, this is a base32 hash of several fields
  :var int version: **\\*** hidden service descriptor version
  :var str permanent_key: **\\*** long term key of the hidden service
  :var str secret_id_part: **\\*** hash of the time period, cookie, and replica
    values so our descriptor_id can be validated
  :var datetime published: **\\*** time in UTC when this descriptor was made
  :var list protocol_versions: **\\*** list of **int** versions that are supported when establishing a connection
  :var str introduction_points_encoded: raw introduction points blob
  :var list introduction_points_auth: **\\*** tuples of the form
    (auth_method, auth_data) for our introduction_points_content
    (**deprecated**, always **[]**)
  :var bytes introduction_points_content: decoded introduction-points content
    without authentication data, if using cookie authentication this is
    encrypted
  :var str signature: signature of the descriptor content

  **\\*** attribute is either required when we're parsed with validation or has
  a default value, others are left as **None** if undefined

  .. versionchanged:: 1.6.0
     Moved from the deprecated `pycrypto
     <https://www.dlitz.net/software/pycrypto/>`_ module to `cryptography
     <https://pypi.org/project/cryptography/>`_ for validating signatures.

  .. versionchanged:: 1.6.0
     Added the **skip_crypto_validation** constructor argument.
  """

  TYPE_ANNOTATION_NAME = 'hidden-service-descriptor'

  ATTRIBUTES = {
    'descriptor_id': (None, _parse_rendezvous_service_descriptor_line),
    'version': (None, _parse_v2_version_line),
    'permanent_key': (None, _parse_permanent_key_line),
    'secret_id_part': (None, _parse_secret_id_part_line),
    'published': (None, _parse_publication_time_line),
    'protocol_versions': ([], _parse_protocol_versions_line),
    'introduction_points_encoded': (None, _parse_introduction_points_line),
    'introduction_points_auth': ([], _parse_introduction_points_line),
    'introduction_points_content': (None, _parse_introduction_points_line),
    'signature': (None, _parse_v2_signature_line),
  }

  PARSER_FOR_LINE = {
    'rendezvous-service-descriptor': _parse_rendezvous_service_descriptor_line,
    'version': _parse_v2_version_line,
    'permanent-key': _parse_permanent_key_line,
    'secret-id-part': _parse_secret_id_part_line,
    'publication-time': _parse_publication_time_line,
    'protocol-versions': _parse_protocol_versions_line,
    'introduction-points': _parse_introduction_points_line,
    'signature': _parse_v2_signature_line,
  }

  @classmethod
  def content(cls, attr = None, exclude = (), sign = False):
    if sign:
      raise NotImplementedError('Signing of %s not implemented' % cls.__name__)

    return _descriptor_content(attr, exclude, (
      ('rendezvous-service-descriptor', 'y3olqqblqw2gbh6phimfuiroechjjafa'),
      ('version', '2'),
      ('permanent-key', _random_crypto_blob('RSA PUBLIC KEY')),
      ('secret-id-part', 'e24kgecavwsznj7gpbktqsiwgvngsf4e'),
      ('publication-time', _random_date()),
      ('protocol-versions', '2,3'),
      ('introduction-points', '\n-----BEGIN MESSAGE-----\n-----END MESSAGE-----'),
    ), (
      ('signature', _random_crypto_blob('SIGNATURE')),
    ))

  @classmethod
  def create(cls, attr = None, exclude = (), validate = True, sign = False):
    return cls(cls.content(attr, exclude, sign), validate = validate, skip_crypto_validation = not sign)

  def __init__(self, raw_contents, validate = False, skip_crypto_validation = False):
    super(HiddenServiceDescriptorV2, self).__init__(raw_contents, lazy_load = not validate)
    entries = _descriptor_components(raw_contents, validate, non_ascii_fields = ('introduction-points'))

    if validate:
      for keyword in REQUIRED_V2_FIELDS:
        if keyword not in entries:
          raise ValueError("Hidden service descriptor must have a '%s' entry" % keyword)
        elif keyword in entries and len(entries[keyword]) > 1:
          raise ValueError("The '%s' entry can only appear once in a hidden service descriptor" % keyword)

      if 'rendezvous-service-descriptor' != list(entries.keys())[0]:
        raise ValueError("Hidden service descriptor must start with a 'rendezvous-service-descriptor' entry")
      elif 'signature' != list(entries.keys())[-1]:
        raise ValueError("Hidden service descriptor must end with a 'signature' entry")

      self._parse(entries, validate)

      if not skip_crypto_validation and stem.prereq.is_crypto_available():
        signed_digest = self._digest_for_signature(self.permanent_key, self.signature)
        digest_content = self._content_range('rendezvous-service-descriptor ', '\nsignature\n')
        content_digest = hashlib.sha1(digest_content).hexdigest().upper()

        if signed_digest != content_digest:
          raise ValueError('Decrypted digest does not match local digest (calculated: %s, local: %s)' % (signed_digest, content_digest))
    else:
      self._entries = entries

  @lru_cache()
  def introduction_points(self, authentication_cookie = None):
    """
    Provided this service's introduction points.

    :returns: **list** of :class:`~stem.descriptor.hidden_service.IntroductionPoints`

    :raises:
      * **ValueError** if the our introduction-points is malformed
      * **DecryptionFailure** if unable to decrypt this field
    """

    content = self.introduction_points_content

    if not content:
      return []
    elif authentication_cookie:
      if not stem.prereq.is_crypto_available():
        raise DecryptionFailure('Decrypting introduction-points requires the cryptography module')

      try:
        authentication_cookie = stem.util.str_tools._decode_b64(authentication_cookie)
      except TypeError as exc:
        raise DecryptionFailure('authentication_cookie must be a base64 encoded string (%s)' % exc)

      authentication_type = int(binascii.hexlify(content[0:1]), 16)

      if authentication_type == BASIC_AUTH:
        content = HiddenServiceDescriptorV2._decrypt_basic_auth(content, authentication_cookie)
      elif authentication_type == STEALTH_AUTH:
        content = HiddenServiceDescriptorV2._decrypt_stealth_auth(content, authentication_cookie)
      else:
        raise DecryptionFailure("Unrecognized authentication type '%s', currently we only support basic auth (%s) and stealth auth (%s)" % (authentication_type, BASIC_AUTH, STEALTH_AUTH))

      if not content.startswith(b'introduction-point '):
        raise DecryptionFailure('Unable to decrypt the introduction-points, maybe this is the wrong key?')
    elif not content.startswith(b'introduction-point '):
      raise DecryptionFailure('introduction-points content is encrypted, you need to provide its authentication_cookie')

    return HiddenServiceDescriptorV2._parse_introduction_points(content)

  @staticmethod
  def _decrypt_basic_auth(content, authentication_cookie):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    try:
      client_blocks = int(binascii.hexlify(content[1:2]), 16)
    except ValueError:
      raise DecryptionFailure("When using basic auth the content should start with a number of blocks but wasn't a hex digit: %s" % binascii.hexlify(content[1:2]))

    # parse the client id and encrypted session keys

    client_entries_length = client_blocks * 16 * 20
    client_entries = content[2:2 + client_entries_length]
    client_keys = [(client_entries[i:i + 4], client_entries[i + 4:i + 20]) for i in range(0, client_entries_length, 4 + 16)]

    iv = content[2 + client_entries_length:2 + client_entries_length + 16]
    encrypted = content[2 + client_entries_length + 16:]

    client_id = hashlib.sha1(authentication_cookie + iv).digest()[:4]

    for entry_id, encrypted_session_key in client_keys:
      if entry_id != client_id:
        continue  # not the session key for this client

      # try decrypting the session key

      cipher = Cipher(algorithms.AES(authentication_cookie), modes.CTR(b'\x00' * len(iv)), default_backend())
      decryptor = cipher.decryptor()
      session_key = decryptor.update(encrypted_session_key) + decryptor.finalize()

      # attempt to decrypt the intro points with the session key

      cipher = Cipher(algorithms.AES(session_key), modes.CTR(iv), default_backend())
      decryptor = cipher.decryptor()
      decrypted = decryptor.update(encrypted) + decryptor.finalize()

      # check if the decryption looks correct

      if decrypted.startswith(b'introduction-point '):
        return decrypted

    return content  # nope, unable to decrypt the content

  @staticmethod
  def _decrypt_stealth_auth(content, authentication_cookie):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    # byte 1 = authentication type, 2-17 = input vector, 18 on = encrypted content
    iv, encrypted = content[1:17], content[17:]
    cipher = Cipher(algorithms.AES(authentication_cookie), modes.CTR(iv), default_backend())
    decryptor = cipher.decryptor()

    return decryptor.update(encrypted) + decryptor.finalize()

  @staticmethod
  def _parse_introduction_points(content):
    """
    Provides the parsed list of IntroductionPoints for the unencrypted content.
    """

    introduction_points = []
    content_io = io.BytesIO(content)

    while True:
      content = b''.join(_read_until_keywords('introduction-point', content_io, ignore_first = True))

      if not content:
        break  # reached the end

      attr = dict(INTRODUCTION_POINTS_ATTR)
      entries = _descriptor_components(content, False)

      for keyword, values in list(entries.items()):
        value, block_type, block_contents = values[0]

        if keyword in SINGLE_INTRODUCTION_POINT_FIELDS and len(values) > 1:
          raise ValueError("'%s' can only appear once in an introduction-point block, but appeared %i times" % (keyword, len(values)))

        if keyword == 'introduction-point':
          attr['identifier'] = value
        elif keyword == 'ip-address':
          if not stem.util.connection.is_valid_ipv4_address(value):
            raise ValueError("'%s' is an invalid IPv4 address" % value)

          attr['address'] = value
        elif keyword == 'onion-port':
          if not stem.util.connection.is_valid_port(value):
            raise ValueError("'%s' is an invalid port" % value)

          attr['port'] = int(value)
        elif keyword == 'onion-key':
          attr['onion_key'] = block_contents
        elif keyword == 'service-key':
          attr['service_key'] = block_contents
        elif keyword == 'intro-authentication':
          auth_entries = []

          for auth_value, _, _ in values:
            if ' ' not in auth_value:
              raise ValueError("We expected 'intro-authentication [auth_type] [auth_data]', but had '%s'" % auth_value)

            auth_type, auth_data = auth_value.split(' ')[:2]
            auth_entries.append((auth_type, auth_data))

      introduction_points.append(IntroductionPoints(**attr))

    return introduction_points


def _get_descriptor_signing_cert(descriptor_signing_public_key, blinded_priv_key):
  """
  Get the string representation of the descriptor signing cert

  'descriptor_signing_public_key' key that gets certified by certificate

  'blinded_priv_key' key that signs the certificate
  """

  from cryptography.hazmat.primitives import serialization

  # 54 hours expiration date like tor does
  expiration_date = datetime.datetime.utcnow() + datetime.timedelta(hours=54)

  signing_key = descriptor_signing_public_key.public_bytes(encoding = serialization.Encoding.Raw, format = serialization.PublicFormat.Raw)
  extensions = [Ed25519Extension(ExtensionType.HAS_SIGNING_KEY, None, blinded_priv_key.public_key().public_bytes(encoding = serialization.Encoding.Raw, format = serialization.PublicFormat.Raw))]

  desc_signing_cert = Ed25519CertificateV1(CertType.HS_V3_DESC_SIGNING, expiration_date, 1, signing_key, extensions, signing_key = blinded_priv_key)

  return '\n' + desc_signing_cert.to_base64(pem = True)


def b64_and_wrap_desc_layer(layer_bytes, prefix_bytes=b''):
  """
  Encode the descriptor layer in 'layer_bytes' to base64, and then wrap it up
  so that it can be included in the descriptor.
  """

  layer_b64 = base64.b64encode(layer_bytes)
  layer_blob = b'\n'.join(stem.util.str_tools._split_by_length(layer_b64, 64))

  return b'%s\n-----BEGIN MESSAGE-----\n%s\n-----END MESSAGE-----' % (prefix_bytes, layer_blob)


def _get_inner_descriptor_layer_body(intro_points, descriptor_signing_privkey):
  """
  Get the inner descriptor layer into bytes.

  'intro_points' is the list of introduction points that should be embedded to
  this layer, and we also need the descriptor signing key to encode the intro
  points.
  """

  if (not intro_points or len(intro_points) == 0):
    raise ValueError('Need a proper intro points set')

  final_body = b'create2-formats 2\n'

  # Now encode all the intro points
  for intro_point in intro_points:
    final_body += intro_point.encode() + b'\n'

  return final_body


def _get_fake_clients_bytes():
  """
  Generate fake client authorization data for the middle layer
  """

  final_bytes = b''
  num_fake_clients = 16  # default for when client auth is disabled

  for _ in range(num_fake_clients):
    client_id = base64.b64encode(os.urandom(8)).rstrip(b'=')
    client_iv = base64.b64encode(os.urandom(16)).rstrip(b'=')
    descriptor_cookie = base64.b64encode(os.urandom(16)).rstrip(b'=')

    final_bytes += b'%s %s %s %s\n' % (b'auth-client', client_id, client_iv, descriptor_cookie)

  return final_bytes


def _get_middle_descriptor_layer_body(encrypted):
  """
  Get the middle descriptor layer as bytes
  (It's just fake client auth data since client auth is disabled)
  """

  from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
  from cryptography.hazmat.primitives import serialization

  fake_pub_key = X25519PrivateKey.generate().public_key()
  fake_pub_key_bytes = fake_pub_key.public_bytes(encoding = serialization.Encoding.Raw, format = serialization.PublicFormat.Raw)
  fake_pub_key_bytes_b64 = base64.b64encode(fake_pub_key_bytes)
  fake_clients = _get_fake_clients_bytes()

  return b'desc-auth-type x25519\n' \
    b'desc-auth-ephemeral-key %s\n' \
    b'%s' \
    b'%s' % (fake_pub_key_bytes_b64, fake_clients, encrypted)


def _get_superencrypted_blob(intro_points, descriptor_signing_privkey, revision_counter, blinded_key_bytes, subcredential):
  """
  Get the superencrypted blob (which also includes the encrypted blob) that
  should be attached to the descriptor
  """

  inner_descriptor_layer = stem.util.str_tools._to_bytes(_get_inner_descriptor_layer_body(intro_points, descriptor_signing_privkey))
  inner_ciphertext = stem.descriptor.hsv3_crypto.encrypt_inner_layer(inner_descriptor_layer, revision_counter, blinded_key_bytes, subcredential)
  inner_ciphertext_b64 = b64_and_wrap_desc_layer(inner_ciphertext, b'encrypted')

  middle_descriptor_layer = _get_middle_descriptor_layer_body(inner_ciphertext_b64)
  outter_ciphertext = stem.descriptor.hsv3_crypto.encrypt_outter_layer(middle_descriptor_layer, revision_counter, blinded_key_bytes, subcredential)

  return b64_and_wrap_desc_layer(outter_ciphertext)


class HiddenServiceDescriptorV3(BaseHiddenServiceDescriptor):
  """
  Version 3 hidden service descriptor.

  :var int version: **\\*** hidden service descriptor version
  :var int lifetime: **\\*** minutes after publication this descriptor is valid
  :var stem.descriptor.certificate.Ed25519Certificate signing_cert: **\\*** cross-certifier for the short-term descriptor signing key
  :var int revision_counter: **\\*** descriptor revision number
  :var str superencrypted: **\\*** encrypted HS-DESC-ENC payload
  :var str signature: **\\*** signature of this descriptor

  **\\*** attribute is either required when we're parsed with validation or has
  a default value, others are left as **None** if undefined

  .. versionadded:: 1.8.0
  """

  # TODO: requested this @type on https://trac.torproject.org/projects/tor/ticket/31481

  TYPE_ANNOTATION_NAME = 'hidden-service-descriptor-3'

  ATTRIBUTES = {
    'version': (None, _parse_v3_version_line),
    'lifetime': (None, _parse_lifetime_line),
    'signing_cert': (None, _parse_signing_cert),
    'revision_counter': (None, _parse_revision_counter_line),
    'superencrypted': (None, _parse_superencrypted_line),
    'signature': (None, _parse_v3_signature_line),
  }

  PARSER_FOR_LINE = {
    'hs-descriptor': _parse_v3_version_line,
    'descriptor-lifetime': _parse_lifetime_line,
    'descriptor-signing-key-cert': _parse_signing_cert,
    'revision-counter': _parse_revision_counter_line,
    'superencrypted': _parse_superencrypted_line,
    'signature': _parse_v3_signature_line,
  }

  @classmethod
  def content(cls, attr = None, exclude = (), sign = False, ed25519_private_identity_key = None, intro_points = None, blinding_param = None):
    """
    'ed25519_private_identity_key' is the Ed25519PrivateKey  of the onion service

    'intro_points' is a list of IntroductionPointV3 objects

    'blinding_param' is a 32 byte blinding factor that should be used to derive
    the blinded key from the identity key
    """

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    if sign:
      raise NotImplementedError('Signing of %s not implemented' % cls.__name__)

    # TODO: When these additional arguments are not present simply constructing
    # a descriptor that looks reasonable. This might not be the fallback we use
    # later on.

    if not ed25519_private_identity_key or not intro_points or not blinding_param:
      return _descriptor_content(attr, exclude, (
        ('hs-descriptor', '3'),
        ('descriptor-lifetime', '180'),
        ('descriptor-signing-key-cert', _random_crypto_blob('ED25519 CERT')),
        ('revision-counter', '15'),
        ('superencrypted', _random_crypto_blob('MESSAGE')),
        ('signature', 'wdc7ffr+dPZJ/mIQ1l4WYqNABcmsm6SHW/NL3M3wG7bjjqOJWoPR5TimUXxH52n5Zk0Gc7hl/hz3YYmAx5MvAg'),
      ), ())

    # We need an private identity key for the onion service to create its
    # descriptor. We could make a new one on the spot, but we also need to
    # return it to the caller, otherwise the caller will have no way to decode
    # the descriptor without knowing the private key or the onion address, so
    # for now we consider it a mandatory argument.

    if not ed25519_private_identity_key:
      raise ValueError('Need to provide a private ed25519 identity key to create a descriptor')

    if not intro_points:
      raise ValueError('Need to provide the introduction points for this descriptor')

    if not blinding_param:
      raise ValueError('Need to provide a blinding param for this descriptor')

    # Get the identity public key
    public_identity_key = ed25519_private_identity_key.public_key()
    public_identity_key_bytes = public_identity_key.public_bytes(encoding = serialization.Encoding.Raw, format = serialization.PublicFormat.Raw)

    # Blind the identity key to get ephemeral blinded key
    blinded_privkey = stem.descriptor.hsv3_crypto.HSv3PrivateBlindedKey(ed25519_private_identity_key, blinding_param = blinding_param)
    blinded_pubkey = blinded_privkey.public_key()
    blinded_pubkey_bytes = blinded_pubkey.public_bytes(encoding = serialization.Encoding.Raw, format = serialization.PublicFormat.Raw)

    # Generate descriptor signing key
    signing_key = Ed25519PrivateKey.generate()
    descriptor_signing_public_key = signing_key.public_key()

    # Get the main encrypted descriptor body
    revision_counter_int = int(time.time())
    subcredential = HiddenServiceDescriptorV3._subcredential(public_identity_key_bytes, blinded_pubkey_bytes)

    # XXX It would be more elegant to have all the above variables attached to
    # this descriptor object so that we don't have to carry them around
    # functions and instead we could use e.g. self.descriptor_signing_public_key
    # But because this is a @classmethod this is not possible :/
    superencrypted_blob = _get_superencrypted_blob(intro_points, signing_key, revision_counter_int, blinded_pubkey_bytes, subcredential)

    desc_content = _descriptor_content(attr, exclude, (
      ('hs-descriptor', '3'),
      ('descriptor-lifetime', '180'),
      ('descriptor-signing-key-cert', _get_descriptor_signing_cert(descriptor_signing_public_key, blinded_privkey)),
      ('revision-counter', str(revision_counter_int)),
      ('superencrypted', superencrypted_blob),
    ), ())

    # Add a final newline before the signature block
    desc_content += b'\n'

    sig_content = stem.descriptor.certificate.SIG_PREFIX_HS_V3 + desc_content
    signature = b'signature %s' % base64.b64encode(signing_key.sign(sig_content)).rstrip(b'=')

    return desc_content + signature

  @classmethod
  def create(cls, attr = None, exclude = (), validate = True, sign = False):
    return cls(cls.content(attr, exclude, sign), validate = validate, skip_crypto_validation = not sign)

  def __init__(self, raw_contents, validate = False):
    super(HiddenServiceDescriptorV3, self).__init__(raw_contents, lazy_load = not validate)

    self._inner_layer = None
    entries = _descriptor_components(raw_contents, validate)

    if validate:
      for keyword in REQUIRED_V3_FIELDS:
        if keyword not in entries:
          raise ValueError("Hidden service descriptor must have a '%s' entry" % keyword)
        elif keyword in entries and len(entries[keyword]) > 1:
          raise ValueError("The '%s' entry can only appear once in a hidden service descriptor" % keyword)

      if 'hs-descriptor' != list(entries.keys())[0]:
        raise ValueError("Hidden service descriptor must start with a 'hs-descriptor' entry")
      elif 'signature' != list(entries.keys())[-1]:
        raise ValueError("Hidden service descriptor must end with a 'signature' entry")

      self._parse(entries, validate)

      if self.signing_cert and stem.prereq.is_crypto_available(ed25519 = True):
        self.signing_cert.validate(self)
    else:
      self._entries = entries

  def decrypt(self, onion_address):
    """
    Decrypt this descriptor. Hidden serice descriptors contain two encryption
    layers (:class:`~stem.descriptor.hidden_service.OuterLayer` and
    :class:`~stem.descriptor.hidden_service.InnerLayer`).

    :param str onion_address: hidden service address this descriptor is from

    :returns: :class:`~stem.descriptor.hidden_service.InnerLayer` with our
      decrypted content

    :raises:
      * **ImportError** if required cryptography or sha3 module is unavailable
      * **ValueError** if unable to decrypt or validation fails
    """

    if not stem.prereq.is_crypto_available(ed25519 = True):
      raise ImportError('Hidden service descriptor decryption requires cryptography version 2.6')
    elif not stem.prereq._is_sha3_available():
      raise ImportError('Hidden service descriptor decryption requires python 3.6+ or the pysha3 module (https://pypi.org/project/pysha3/)')

    if self._inner_layer is None:
      blinded_key = self.signing_cert.signing_key() if self.signing_cert else None

      if not blinded_key:
        raise ValueError('No signing key is present')

      identity_public_key = HiddenServiceDescriptorV3.public_key_from_address(onion_address)
      subcredential = HiddenServiceDescriptorV3._subcredential(identity_public_key, blinded_key)

      outer_layer = OuterLayer._decrypt(self.superencrypted, self.revision_counter, subcredential, blinded_key)
      self._inner_layer = InnerLayer._decrypt(outer_layer, self.revision_counter, subcredential, blinded_key)

    return self._inner_layer

  @staticmethod
  def address_from_public_key(pubkey, suffix = True):
    """
    Converts a hidden service public key into its address.

    :param bytes pubkey: hidden service public key
    :param bool suffix: includes the '.onion' suffix if true, excluded otherwise

    :returns: **unicode** hidden service address

    :raises: **ImportError** if sha3 unsupported
    """

    if not stem.prereq._is_sha3_available():
      raise ImportError('Hidden service address conversion requires python 3.6+ or the pysha3 module (https://pypi.org/project/pysha3/)')

    version = stem.client.datatype.Size.CHAR.pack(3)
    checksum = hashlib.sha3_256(CHECKSUM_CONSTANT + pubkey + version).digest()[:2]
    onion_address = base64.b32encode(pubkey + checksum + version)

    return stem.util.str_tools._to_unicode(onion_address + b'.onion' if suffix else onion_address).lower()

  @staticmethod
  def public_key_from_address(onion_address):
    """
    Converts a hidden service address into its public key.

    :param str onion_address: hidden service address

    :returns: **bytes** for the hidden service's public key

    :raises:
      * **ImportError** if sha3 unsupported
      * **ValueError** if address malformed or checksum is invalid
    """

    if not stem.prereq._is_sha3_available():
      raise ImportError('Hidden service address conversion requires python 3.6+ or the pysha3 module (https://pypi.org/project/pysha3/)')

    if onion_address.endswith('.onion'):
      onion_address = onion_address[:-6]

    if not stem.util.tor_tools.is_valid_hidden_service_address(onion_address, version = 3):
      raise ValueError("'%s.onion' isn't a valid hidden service v3 address" % onion_address)

    # onion_address = base32(PUBKEY | CHECKSUM | VERSION) + '.onion'
    # CHECKSUM = H('.onion checksum' | PUBKEY | VERSION)[:2]

    decoded_address = base64.b32decode(onion_address.upper())

    pubkey = decoded_address[:32]
    expected_checksum = decoded_address[32:34]
    version = decoded_address[34:35]

    checksum = hashlib.sha3_256(CHECKSUM_CONSTANT + pubkey + version).digest()[:2]

    if expected_checksum != checksum:
      checksum_str = stem.util.str_tools._to_unicode(binascii.hexlify(checksum))
      expected_checksum_str = stem.util.str_tools._to_unicode(binascii.hexlify(expected_checksum))

      raise ValueError('Bad checksum (expected %s but was %s)' % (expected_checksum_str, checksum_str))

    return pubkey

  @staticmethod
  def _subcredential(public_key, blinded_key):
    # credential = H('credential' | public-identity-key)
    # subcredential = H('subcredential' | credential | blinded-public-key)

    credential = hashlib.sha3_256(b'credential%s' % public_key).digest()
    return hashlib.sha3_256(b'subcredential%s%s' % (credential, blinded_key)).digest()


class OuterLayer(Descriptor):
  """
  Initial encryped layer of a hidden service v3 descriptor (`spec
  <https://gitweb.torproject.org/torspec.git/tree/rend-spec-v3.txt#n1154>`_).

  .. versionadded:: 1.8.0

  :var str auth_type: **\\*** encryption scheme used for descriptor authorization
  :var str ephemeral_key: **\\*** base64 encoded x25519 public key
  :var dict clients: **\\*** mapping of authorized client ids to their
    :class:`~stem.descriptor.hidden_service.AuthorizedClient`
  :var str encrypted: **\\*** encrypted descriptor inner layer

  **\\*** attribute is either required when we're parsed with validation or has
  a default value, others are left as **None** if undefined
  """

  ATTRIBUTES = {
    'auth_type': (None, _parse_v3_outer_auth_type),
    'ephemeral_key': (None, _parse_v3_outer_ephemeral_key),
    'clients': ({}, _parse_v3_outer_clients),
    'encrypted': (None, _parse_v3_outer_encrypted),
  }

  PARSER_FOR_LINE = {
    'desc-auth-type': _parse_v3_outer_auth_type,
    'desc-auth-ephemeral-key': _parse_v3_outer_ephemeral_key,
    'auth-client': _parse_v3_outer_clients,
    'encrypted': _parse_v3_outer_encrypted,
  }

  @staticmethod
  def _decrypt(encrypted, revision_counter, subcredential, blinded_key):
    plaintext = _decrypt_layer(encrypted, b'hsdir-superencrypted-data', revision_counter, subcredential, blinded_key)
    return OuterLayer(plaintext)

  def __init__(self, content, validate = False):
    content = content.rstrip('\x00')  # strip null byte padding

    super(OuterLayer, self).__init__(content, lazy_load = not validate)
    entries = _descriptor_components(content, validate)

    if validate:
      self._parse(entries, validate)
    else:
      self._entries = entries


class InnerLayer(Descriptor):
  """
  Second encryped layer of a hidden service v3 descriptor (`spec
  <https://gitweb.torproject.org/torspec.git/tree/rend-spec-v3.txt#n1308>`_).

  .. versionadded:: 1.8.0

  :var stem.descriptor.hidden_service.OuterLayer outer: enclosing encryption layer

  :var list formats: **\\*** recognized CREATE2 cell formats
  :var list intro_auth: **\\*** introduction-layer authentication types
  :var bool is_single_service: **\\*** **True** if this is a `single onion service <https://gitweb.torproject.org/torspec.git/tree/proposals/260-rend-single-onion.txt>`_, **False** otherwise
  :var list introduction_points: :class:`~stem.descriptor.hidden_service.IntroductionPointV3` where this service is reachable

  **\\*** attribute is either required when we're parsed with validation or has
  a default value, others are left as **None** if undefined
  """

  ATTRIBUTES = {
    'formats': ([], _parse_v3_inner_formats),
    'intro_auth': ([], _parse_v3_inner_intro_auth),
    'is_single_service': (False, _parse_v3_inner_single_service),
    'introduction_points': ([], _parse_v3_introduction_points),
  }

  PARSER_FOR_LINE = {
    'create2-formats': _parse_v3_inner_formats,
    'intro-auth-required': _parse_v3_inner_intro_auth,
    'single-onion-service': _parse_v3_inner_single_service,
  }

  @staticmethod
  def _decrypt(outer_layer, revision_counter, subcredential, blinded_key):
    plaintext = _decrypt_layer(outer_layer.encrypted, b'hsdir-encrypted-data', revision_counter, subcredential, blinded_key)
    return InnerLayer(plaintext, validate = True, outer_layer = outer_layer)

  def __init__(self, content, validate = False, outer_layer = None):
    super(InnerLayer, self).__init__(content, lazy_load = not validate)
    self.outer = outer_layer

    # inner layer begins with a few header fields, followed by multiple any
    # number of introduction-points

    div = content.find('\nintroduction-point ')

    if div != -1:
      self._unparsed_introduction_points = content[div + 1:]
      content = content[:div]
    else:
      self._unparsed_introduction_points = None

    entries = _descriptor_components(content, validate)

    if validate:
      self._parse(entries, validate)
      _parse_v3_introduction_points(self, entries)
    else:
      self._entries = entries


# TODO: drop this alias in stem 2.x

HiddenServiceDescriptor = HiddenServiceDescriptorV2
