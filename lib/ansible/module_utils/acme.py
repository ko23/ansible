# -*- coding: utf-8 -*-

# This code is part of Ansible, but is an independent component.
# This particular file snippet, and this file snippet only, is BSD licensed.
# Modules you write using this snippet, which is embedded dynamically by Ansible
# still belong to the author of the module, and may assign their own license
# to the complete work.
#
# Copyright (c), Michael Gruener <michael.gruener@chaosmoon.net>, 2016
#
# Simplified BSD License (see licenses/simplified_bsd.txt or https://opensource.org/licenses/BSD-2-Clause)

from __future__ import absolute_import, division, print_function
__metaclass__ = type


import base64
import binascii
import copy
import datetime
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import traceback

from ansible.module_utils._text import to_native, to_text, to_bytes
from ansible.module_utils.urls import fetch_url as _fetch_url

try:
    import cryptography
    import cryptography.hazmat.backends
    import cryptography.hazmat.primitives.serialization
    import cryptography.hazmat.primitives.asymmetric.rsa
    import cryptography.hazmat.primitives.asymmetric.ec
    import cryptography.hazmat.primitives.asymmetric.padding
    import cryptography.hazmat.primitives.hashes
    import cryptography.hazmat.primitives.asymmetric.utils
    import cryptography.x509
    import cryptography.x509.oid
    from distutils.version import LooseVersion
    CRYPTOGRAPHY_VERSION = cryptography.__version__
    HAS_CURRENT_CRYPTOGRAPHY = (LooseVersion(CRYPTOGRAPHY_VERSION) >= LooseVersion('1.5'))
    if HAS_CURRENT_CRYPTOGRAPHY:
        _cryptography_backend = cryptography.hazmat.backends.default_backend()
except Exception as _:
    HAS_CURRENT_CRYPTOGRAPHY = False


class ModuleFailException(Exception):
    '''
    If raised, module.fail_json() will be called with the given parameters after cleanup.
    '''
    def __init__(self, msg, **args):
        super(ModuleFailException, self).__init__(self, msg)
        self.msg = msg
        self.module_fail_args = args

    def do_fail(self, module):
        module.fail_json(msg=self.msg, other=self.module_fail_args)


def _lowercase_fetch_url(*args, **kwargs):
    '''
     Add lowercase representations of the header names as dict keys

    '''
    response, info = _fetch_url(*args, **kwargs)

    info.update(dict((header.lower(), value) for (header, value) in info.items()))
    return response, info


fetch_url = _lowercase_fetch_url


def nopad_b64(data):
    return base64.urlsafe_b64encode(data).decode('utf8').replace("=", "")


def simple_get(module, url):
    resp, info = fetch_url(module, url, method='GET')

    result = {}
    try:
        content = resp.read()
    except AttributeError:
        content = info.get('body')

    if content:
        if info['content-type'].startswith('application/json'):
            try:
                result = module.from_json(content.decode('utf8'))
            except ValueError:
                raise ModuleFailException("Failed to parse the ACME response: {0} {1}".format(url, content))
        else:
            result = content

    if info['status'] >= 400:
        raise ModuleFailException("ACME request failed: CODE: {0} RESULT: {1}".format(info['status'], result))
    return result


def read_file(fn, mode='b'):
    try:
        with open(fn, 'r' + mode) as f:
            return f.read()
    except Exception as e:
        raise ModuleFailException('Error while reading file "{0}": {1}'.format(fn, e))


# function source: network/basics/uri.py
def write_file(module, dest, content):
    '''
    Write content to destination file dest, only if the content
    has changed.
    '''
    changed = False
    # create a tempfile
    fd, tmpsrc = tempfile.mkstemp(text=False)
    f = os.fdopen(fd, 'wb')
    try:
        f.write(content)
    except Exception as err:
        try:
            f.close()
        except Exception as e:
            pass
        os.remove(tmpsrc)
        raise ModuleFailException("failed to create temporary content file: %s" % to_native(err), exception=traceback.format_exc())
    f.close()
    checksum_src = None
    checksum_dest = None
    # raise an error if there is no tmpsrc file
    if not os.path.exists(tmpsrc):
        try:
            os.remove(tmpsrc)
        except Exception as e:
            pass
        raise ModuleFailException("Source %s does not exist" % (tmpsrc))
    if not os.access(tmpsrc, os.R_OK):
        os.remove(tmpsrc)
        raise ModuleFailException("Source %s not readable" % (tmpsrc))
    checksum_src = module.sha1(tmpsrc)
    # check if there is no dest file
    if os.path.exists(dest):
        # raise an error if copy has no permission on dest
        if not os.access(dest, os.W_OK):
            os.remove(tmpsrc)
            raise ModuleFailException("Destination %s not writable" % (dest))
        if not os.access(dest, os.R_OK):
            os.remove(tmpsrc)
            raise ModuleFailException("Destination %s not readable" % (dest))
        checksum_dest = module.sha1(dest)
    else:
        if not os.access(os.path.dirname(dest), os.W_OK):
            os.remove(tmpsrc)
            raise ModuleFailException("Destination dir %s not writable" % (os.path.dirname(dest)))
    if checksum_src != checksum_dest:
        try:
            shutil.copyfile(tmpsrc, dest)
            changed = True
        except Exception as err:
            os.remove(tmpsrc)
            raise ModuleFailException("failed to copy %s to %s: %s" % (tmpsrc, dest, to_native(err)), exception=traceback.format_exc())
    os.remove(tmpsrc)
    return changed


def pem_to_der(pem_filename):
    '''
    Load PEM file, and convert to DER.

    If PEM contains multiple entities, the first entity will be used.
    '''
    certificate_lines = []
    try:
        with open(pem_filename, "rt") as f:
            header_line_count = 0
            for line in f:
                if line.startswith('-----'):
                    header_line_count += 1
                    if header_line_count == 2:
                        # If certificate file contains other certs appended
                        # (like intermediate certificates), ignore these.
                        break
                    continue
                certificate_lines.append(line.strip())
    except Exception as err:
        raise ModuleFailException("cannot load PEM file {0}: {1}".format(pem_filename, to_native(err)), exception=traceback.format_exc())
    return base64.b64decode(''.join(certificate_lines))


def _parse_key_openssl(openssl_binary, module, key_file=None, key_content=None):
    '''
    Parses an RSA or Elliptic Curve key file in PEM format and returns a pair
    (error, key_data).
    '''
    # If key_file isn't given, but key_content, write that to a temporary file
    if key_file is None:
        fd, tmpsrc = tempfile.mkstemp()
        module.add_cleanup_file(tmpsrc)  # Ansible will delete the file on exit
        f = os.fdopen(fd, 'wb')
        try:
            f.write(key_content.encode('utf-8'))
            key_file = tmpsrc
        except Exception as err:
            try:
                f.close()
            except Exception as e:
                pass
            raise ModuleFailException("failed to create temporary content file: %s" % to_native(err), exception=traceback.format_exc())
        f.close()
    # Parse key
    account_key_type = None
    with open(key_file, "rt") as f:
        for line in f:
            m = re.match(r"^\s*-{5,}BEGIN\s+(EC|RSA)\s+PRIVATE\s+KEY-{5,}\s*$", line)
            if m is not None:
                account_key_type = m.group(1).lower()
                break
    if account_key_type is None:
        # This happens for example if openssl_privatekey created this key
        # (as opposed to the OpenSSL binary). For now, we assume this is
        # an RSA key.
        # FIXME: add some kind of auto-detection
        account_key_type = "rsa"
    if account_key_type not in ("rsa", "ec"):
        return 'unknown key type "%s"' % account_key_type, {}

    openssl_keydump_cmd = [openssl_binary, account_key_type, "-in", key_file, "-noout", "-text"]
    dummy, out, dummy = module.run_command(openssl_keydump_cmd, check_rc=True)

    if account_key_type == 'rsa':
        pub_hex, pub_exp = re.search(
            r"modulus:\n\s+00:([a-f0-9\:\s]+?)\npublicExponent: ([0-9]+)",
            to_text(out, errors='surrogate_or_strict'), re.MULTILINE | re.DOTALL).groups()
        pub_exp = "{0:x}".format(int(pub_exp))
        if len(pub_exp) % 2:
            pub_exp = "0{0}".format(pub_exp)

        return None, {
            'key_file': key_file,
            'type': 'rsa',
            'alg': 'RS256',
            'jwk': {
                "kty": "RSA",
                "e": nopad_b64(binascii.unhexlify(pub_exp.encode("utf-8"))),
                "n": nopad_b64(binascii.unhexlify(re.sub(r"(\s|:)", "", pub_hex).encode("utf-8"))),
            },
            'hash': 'sha256',
        }
    elif account_key_type == 'ec':
        pub_data = re.search(
            r"pub:\s*\n\s+04:([a-f0-9\:\s]+?)\nASN1 OID: (\S+)(?:\nNIST CURVE: (\S+))?",
            to_text(out, errors='surrogate_or_strict'), re.MULTILINE | re.DOTALL)
        if pub_data is None:
            return 'cannot parse elliptic curve key', {}
        pub_hex = binascii.unhexlify(re.sub(r"(\s|:)", "", pub_data.group(1)).encode("utf-8"))
        asn1_oid_curve = pub_data.group(2).lower()
        nist_curve = pub_data.group(3).lower() if pub_data.group(3) else None
        if asn1_oid_curve == 'prime256v1' or nist_curve == 'p-256':
            bits = 256
            alg = 'ES256'
            hash = 'sha256'
            point_size = 32
            curve = 'P-256'
        elif asn1_oid_curve == 'secp384r1' or nist_curve == 'p-384':
            bits = 384
            alg = 'ES384'
            hash = 'sha384'
            point_size = 48
            curve = 'P-384'
        elif asn1_oid_curve == 'secp521r1' or nist_curve == 'p-521':
            # Not yet supported on Let's Encrypt side, see
            # https://github.com/letsencrypt/boulder/issues/2217
            bits = 521
            alg = 'ES512'
            hash = 'sha512'
            point_size = 66
            curve = 'P-521'
        else:
            return 'unknown elliptic curve: %s / %s' % (asn1_oid_curve, nist_curve), {}
        bytes = (bits + 7) // 8
        if len(pub_hex) != 2 * bytes:
            return 'bad elliptic curve point (%s / %s)' % (asn1_oid_curve, nist_curve), {}
        return None, {
            'key_file': key_file,
            'type': 'ec',
            'alg': alg,
            'jwk': {
                "kty": "EC",
                "crv": curve,
                "x": nopad_b64(pub_hex[:bytes]),
                "y": nopad_b64(pub_hex[bytes:]),
            },
            'hash': hash,
            'point_size': point_size,
        }


def _sign_request_openssl(openssl_binary, module, payload64, protected64, key_data):
    openssl_sign_cmd = [openssl_binary, "dgst", "-{0}".format(key_data['hash']), "-sign", key_data['key_file']]
    sign_payload = "{0}.{1}".format(protected64, payload64).encode('utf8')
    dummy, out, dummy = module.run_command(openssl_sign_cmd, data=sign_payload, check_rc=True, binary_data=True)

    if key_data['type'] == 'ec':
        dummy, der_out, dummy = module.run_command(
            [openssl_binary, "asn1parse", "-inform", "DER"],
            data=out, binary_data=True)
        expected_len = 2 * key_data['point_size']
        sig = re.findall(
            r"prim:\s+INTEGER\s+:([0-9A-F]{1,%s})\n" % expected_len,
            to_text(der_out, errors='surrogate_or_strict'))
        if len(sig) != 2:
            raise ModuleFailException(
                "failed to generate Elliptic Curve signature; cannot parse DER output: {0}".format(
                    to_text(der_out, errors='surrogate_or_strict')))
        sig[0] = (expected_len - len(sig[0])) * '0' + sig[0]
        sig[1] = (expected_len - len(sig[1])) * '0' + sig[1]
        out = binascii.unhexlify(sig[0]) + binascii.unhexlify(sig[1])

    return {
        "protected": protected64,
        "payload": payload64,
        "signature": nopad_b64(to_bytes(out)),
    }


if sys.version_info[0] >= 3:
    # Python 3 (and newer)
    def _count_bytes(n):
        return (n.bit_length() + 7) // 8 if n > 0 else 0

    def _convert_int_to_bytes(count, no):
        return no.to_bytes(count, byteorder='big')

    def _pad_hex(n, digits):
        res = hex(n)[2:]
        if len(res) < digits:
            res = '0' * (digits - len(res)) + res
        return res
else:
    # Python 2
    def _count_bytes(n):
        if n <= 0:
            return 0
        h = '%x' % n
        return (len(h) + 1) // 2

    def _convert_int_to_bytes(count, n):
        h = '%x' % n
        if len(h) > 2 * count:
            raise Exception('Number {1} needs more than {0} bytes!'.format(count, n))
        return ('0' * (2 * count - len(h)) + h).decode('hex')

    def _pad_hex(n, digits):
        h = '%x' % n
        if len(h) < digits:
            h = '0' * (digits - len(h)) + h
        return h


def _parse_key_cryptography(module, key_file=None, key_content=None):
    '''
    Parses an RSA or Elliptic Curve key file in PEM format and returns a pair
    (error, key_data).
    '''
    # If key_content isn't given, read key_file
    if key_content is None:
        key_content = read_file(key_file)
    else:
        key_content = to_bytes(key_content)
    # Parse key
    try:
        key = cryptography.hazmat.primitives.serialization.load_pem_private_key(key_content, password=None, backend=_cryptography_backend)
    except Exception as e:
        return 'error while loading key: {0}'.format(e), None
    if isinstance(key, cryptography.hazmat.primitives.asymmetric.rsa.RSAPrivateKey):
        pk = key.public_key().public_numbers()
        return None, {
            'key_obj': key,
            'type': 'rsa',
            'alg': 'RS256',
            'jwk': {
                "kty": "RSA",
                "e": nopad_b64(_convert_int_to_bytes(_count_bytes(pk.e), pk.e)),
                "n": nopad_b64(_convert_int_to_bytes(_count_bytes(pk.n), pk.n)),
            },
            'hash': 'sha256',
        }
    elif isinstance(key, cryptography.hazmat.primitives.asymmetric.ec.EllipticCurvePrivateKey):
        pk = key.public_key().public_numbers()
        if pk.curve.name == 'secp256r1':
            bits = 256
            alg = 'ES256'
            hash = 'sha256'
            point_size = 32
            curve = 'P-256'
        elif pk.curve.name == 'secp384r1':
            bits = 384
            alg = 'ES384'
            hash = 'sha384'
            point_size = 48
            curve = 'P-384'
        elif pk.curve.name == 'secp521r1':
            # Not yet supported on Let's Encrypt side, see
            # https://github.com/letsencrypt/boulder/issues/2217
            bits = 521
            alg = 'ES512'
            hash = 'sha512'
            point_size = 66
            curve = 'P-521'
        else:
            return 'unknown elliptic curve: {0}'.format(pk.curve.name), {}
        bytes = (bits + 7) // 8
        return None, {
            'key_obj': key,
            'type': 'ec',
            'alg': alg,
            'jwk': {
                "kty": "EC",
                "crv": curve,
                "x": nopad_b64(_convert_int_to_bytes(bytes, pk.x)),
                "y": nopad_b64(_convert_int_to_bytes(bytes, pk.y)),
            },
            'hash': hash,
            'point_size': point_size,
        }
    else:
        return 'unknown key type "{0}"'.format(type(key)), {}


def _sign_request_cryptography(module, payload64, protected64, key_data):
    sign_payload = "{0}.{1}".format(protected64, payload64).encode('utf8')
    if isinstance(key_data['key_obj'], cryptography.hazmat.primitives.asymmetric.rsa.RSAPrivateKey):
        padding = cryptography.hazmat.primitives.asymmetric.padding.PKCS1v15()
        hash = cryptography.hazmat.primitives.hashes.SHA256()
        signature = key_data['key_obj'].sign(sign_payload, padding, hash)
    elif isinstance(key_data['key_obj'], cryptography.hazmat.primitives.asymmetric.ec.EllipticCurvePrivateKey):
        if key_data['hash'] == 'sha256':
            hash = cryptography.hazmat.primitives.hashes.SHA256
        elif key_data['hash'] == 'sha384':
            hash = cryptography.hazmat.primitives.hashes.SHA384
        elif key_data['hash'] == 'sha512':
            hash = cryptography.hazmat.primitives.hashes.SHA512
        ecdsa = cryptography.hazmat.primitives.asymmetric.ec.ECDSA(hash())
        r, s = cryptography.hazmat.primitives.asymmetric.utils.decode_dss_signature(key_data['key_obj'].sign(sign_payload, ecdsa))
        rr = _pad_hex(r, 2 * key_data['point_size'])
        ss = _pad_hex(s, 2 * key_data['point_size'])
        signature = binascii.unhexlify(rr) + binascii.unhexlify(ss)

    return {
        "protected": protected64,
        "payload": payload64,
        "signature": nopad_b64(signature),
    }


class ACMEDirectory(object):
    '''
    The ACME server directory. Gives access to the available resources,
    and allows to obtain a Replay-Nonce. The acme_directory URL
    needs to support unauthenticated GET requests; ACME endpoints
    requiring authentication are not supported.
    https://tools.ietf.org/html/draft-ietf-acme-acme-14#section-7.1.1
    '''

    def __init__(self, module):
        self.module = module
        self.directory_root = module.params['acme_directory']
        self.version = module.params['acme_version']

        self.directory = simple_get(self.module, self.directory_root)

        # Check whether self.version matches what we expect
        if self.version == 1:
            for key in ('new-reg', 'new-authz', 'new-cert'):
                if key not in self.directory:
                    raise ModuleFailException("ACME directory does not seem to follow protocol ACME v1")
        if self.version == 2:
            for key in ('newNonce', 'newAccount', 'newOrder'):
                if key not in self.directory:
                    raise ModuleFailException("ACME directory does not seem to follow protocol ACME v2")

    def __getitem__(self, key):
        return self.directory[key]

    def get_nonce(self, resource=None):
        url = self.directory_root if self.version == 1 else self.directory['newNonce']
        if resource is not None:
            url = resource
        dummy, info = fetch_url(self.module, url, method='HEAD')
        if info['status'] not in (200, 204):
            raise ModuleFailException("Failed to get replay-nonce, got status {0}".format(info['status']))
        return info['replay-nonce']


class ACMEAccount(object):
    '''
    ACME account object. Handles the authorized communication with the
    ACME server. Provides access to account bound information like
    the currently active authorizations and valid certificates
    '''

    def __init__(self, module):
        self.module = module
        self.version = module.params['acme_version']
        # account_key path and content are mutually exclusive
        self.key = module.params['account_key_src']
        self.key_content = module.params['account_key_content']
        self.directory = ACMEDirectory(module)

        # Grab account URI from module parameters.
        # Make sure empty string is treated as None.
        self.uri = module.params.get('account_uri') or None

        self._openssl_bin = module.get_bin_path('openssl', True)

        if self.key is not None or self.key_content is not None:
            error, self.key_data = self.parse_key(self.key, self.key_content)
            if error:
                raise ModuleFailException("error while parsing account key: %s" % error)
            self.jwk = self.key_data['jwk']
            self.jws_header = {
                "alg": self.key_data['alg'],
                "jwk": self.jwk,
            }
            if self.uri:
                # Make sure self.jws_header is updated
                self.set_account_uri(self.uri)

    def get_keyauthorization(self, token):
        '''
        Returns the key authorization for the given token
        https://tools.ietf.org/html/draft-ietf-acme-acme-14#section-8.1
        '''
        accountkey_json = json.dumps(self.jwk, sort_keys=True, separators=(',', ':'))
        thumbprint = nopad_b64(hashlib.sha256(accountkey_json.encode('utf8')).digest())
        return "{0}.{1}".format(token, thumbprint)

    def parse_key(self, key_file=None, key_content=None):
        '''
        Parses an RSA or Elliptic Curve key file in PEM format and returns a pair
        (error, key_data).
        '''
        if key_file is None and key_content is None:
            raise AssertionError('One of key_file and key_content must be specified!')
        if HAS_CURRENT_CRYPTOGRAPHY:
            return _parse_key_cryptography(self.module, key_file, key_content)
        else:
            return _parse_key_openssl(self._openssl_bin, self.module, key_file, key_content)

    def sign_request(self, protected, payload, key_data):
        try:
            payload64 = nopad_b64(self.module.jsonify(payload).encode('utf8'))
            protected64 = nopad_b64(self.module.jsonify(protected).encode('utf8'))
        except Exception as e:
            raise ModuleFailException("Failed to encode payload / headers as JSON: {0}".format(e))

        if HAS_CURRENT_CRYPTOGRAPHY:
            return _sign_request_cryptography(self.module, payload64, protected64, key_data)
        else:
            return _sign_request_openssl(self._openssl_bin, self.module, payload64, protected64, key_data)

    def send_signed_request(self, url, payload, key_data=None, jws_header=None):
        '''
        Sends a JWS signed HTTP POST request to the ACME server and returns
        the response as dictionary
        https://tools.ietf.org/html/draft-ietf-acme-acme-14#section-6.2
        '''
        key_data = key_data or self.key_data
        jws_header = jws_header or self.jws_header
        failed_tries = 0
        while True:
            protected = copy.deepcopy(jws_header)
            protected["nonce"] = self.directory.get_nonce()
            if self.version != 1:
                protected["url"] = url

            data = self.sign_request(protected, payload, key_data)
            if self.version == 1:
                data["header"] = jws_header
            data = self.module.jsonify(data)

            headers = {
                'Content-Type': 'application/jose+json',
            }
            resp, info = fetch_url(self.module, url, data=data, headers=headers, method='POST')
            result = {}
            try:
                content = resp.read()
            except AttributeError:
                content = info.get('body')

            if content:
                if info['content-type'].startswith('application/json') or 400 <= info['status'] < 600:
                    try:
                        result = self.module.from_json(content.decode('utf8'))
                        # In case of badNonce error, try again (up to 5 times)
                        # (https://tools.ietf.org/html/draft-ietf-acme-acme-14#section-6.6)
                        if (400 <= info['status'] < 600 and
                                result.get('type') == 'urn:ietf:params:acme:error:badNonce' and
                                failed_tries <= 5):
                            failed_tries += 1
                            continue
                    except ValueError:
                        raise ModuleFailException("Failed to parse the ACME response: {0} {1}".format(url, content))
                else:
                    result = content

            return result, info

    def set_account_uri(self, uri):
        '''
        Set account URI. For ACME v2, it needs to be used to sending signed
        requests.
        '''
        self.uri = uri
        if self.version != 1:
            self.jws_header.pop('jwk')
            self.jws_header['kid'] = self.uri

    def _new_reg(self, contact=None, agreement=None, terms_agreed=False, allow_creation=True):
        '''
        Registers a new ACME account. Returns True if the account was
        created and False if it already existed (e.g. it was not newly
        created).
        https://tools.ietf.org/html/draft-ietf-acme-acme-14#section-7.3
        '''
        contact = [] if contact is None else contact

        if self.version == 1:
            new_reg = {
                'resource': 'new-reg',
                'contact': contact
            }
            if agreement:
                new_reg['agreement'] = agreement
            else:
                new_reg['agreement'] = self.directory['meta']['terms-of-service']
            url = self.directory['new-reg']
        else:
            new_reg = {
                'contact': contact
            }
            if not allow_creation:
                new_reg['onlyReturnExisting'] = True
            if terms_agreed:
                new_reg['termsOfServiceAgreed'] = True
            url = self.directory['newAccount']

        result, info = self.send_signed_request(url, new_reg)
        if 'location' in info:
            self.set_account_uri(info['location'])

        if info['status'] in ([200, 201] if self.version == 1 else [201]):
            # Account did not exist
            return True
        elif info['status'] == (409 if self.version == 1 else 200):
            # Account did exist
            return False
        elif info['status'] == 400 and result['type'] == 'urn:ietf:params:acme:error:accountDoesNotExist' and not allow_creation:
            # Account does not exist (and we didn't try to create it)
            return False
        else:
            raise ModuleFailException("Error registering: {0} {1}".format(info['status'], result))

    def get_account_data(self):
        '''
        Retrieve account information. Can only be called when the account
        URI is already known (such as after calling init_account).
        Return None if the account was deactivated, or a dict otherwise.
        '''
        if self.uri is None:
            raise ModuleFailException("Account URI unknown")
        data = {}
        if self.version == 1:
            data['resource'] = 'reg'
        result, info = self.send_signed_request(self.uri, data)
        if info['status'] in (400, 403) and result.get('type') == 'urn:ietf:params:acme:error:unauthorized':
            # Returned when account is deactivated
            return None
        if info['status'] in (400, 404) and result.get('type') == 'urn:ietf:params:acme:error:accountDoesNotExist':
            # Returned when account does not exist
            return None
        if info['status'] < 200 or info['status'] >= 300:
            raise ModuleFailException("Error getting account data from {2}: {0} {1}".format(info['status'], result, self.uri))
        return result

    def init_account(self, contact, agreement=None, terms_agreed=False, allow_creation=True, update_contact=True, remove_account_uri_if_not_exists=False):
        '''
        Create or update an account on the ACME server. For ACME v1,
        as the only way (without knowing an account URI) to test if an
        account exists is to try and create one with the provided account
        key, this method will always result in an account being present
        (except on error situations). For ACME v2, a new account will
        only be created if allow_creation is set to True.

        For ACME v2, check_mode is fully respected. For ACME v1, the account
        might be created if it does not yet exist.

        If the account already exists and if update_contact is set to
        True, this method will update the contact information.

        Return True in case something changed (account was created, contact
        info updated) or would be changed (check_mode). The account URI
        will be stored in self.uri; if it is None, the account does not
        exist.

        https://tools.ietf.org/html/draft-ietf-acme-acme-14#section-7.3
        '''

        new_account = True
        changed = False
        if self.uri is not None:
            new_account = False
            if not update_contact:
                # Verify that the account key belongs to the URI.
                # (If update_contact is True, this will be done below.)
                if self.get_account_data() is None:
                    if remove_account_uri_if_not_exists and not allow_creation:
                        self.uri = None
                        return False
                    raise ModuleFailException("Account is deactivated or does not exist!")
        else:
            new_account = self._new_reg(
                contact,
                agreement=agreement,
                terms_agreed=terms_agreed,
                allow_creation=allow_creation and not self.module.check_mode
            )
            if self.module.check_mode and self.uri is None and allow_creation:
                return True
        if not new_account and self.uri and update_contact:
            result = self.get_account_data()
            if result is None:
                if not allow_creation:
                    self.uri = None
                    return False
                raise ModuleFailException("Account is deactivated or does not exist!")

            # ...and check if update is necessary
            if result.get('contact', []) != contact:
                if not self.module.check_mode:
                    upd_reg = result
                    upd_reg['contact'] = contact
                    result, dummy = self.send_signed_request(self.uri, upd_reg)
                changed = True
        return new_account or changed


def cryptography_get_csr_domains(module, csr_filename):
    '''
    Return a set of requested domains (CN and SANs) for the CSR.
    '''
    domains = set([])
    csr = cryptography.x509.load_pem_x509_csr(read_file(csr_filename), _cryptography_backend)
    for sub in csr.subject:
        if sub.oid == cryptography.x509.oid.NameOID.COMMON_NAME:
            domains.add(sub.value)
    for extension in csr.extensions:
        if extension.oid == cryptography.x509.oid.ExtensionOID.SUBJECT_ALTERNATIVE_NAME:
            for name in extension.value:
                if isinstance(name, cryptography.x509.DNSName):
                    domains.add(name.value)
    return domains


def cryptography_get_cert_days(module, cert_file):
    '''
    Return the days the certificate in cert_file remains valid and -1
    if the file was not found. If cert_file contains more than one
    certificate, only the first one will be considered.
    '''
    if not os.path.exists(cert_file):
        return -1

    try:
        cert = cryptography.x509.load_pem_x509_certificate(read_file(cert_file), _cryptography_backend)
    except Exception as e:
        raise ModuleFailException('Cannot parse certificate {0}: {1}'.format(cert_file, e))
    now = datetime.datetime.now()
    return (cert.not_valid_after - now).days


def set_crypto_backend(module):
    '''
    Sets which crypto backend to use (default: auto detection).

    Does not care whether a new enough cryptoraphy is available or not. Must
    be called before any real stuff is done which might evaluate
    ``HAS_CURRENT_CRYPTOGRAPHY``.
    '''
    global HAS_CURRENT_CRYPTOGRAPHY
    # Choose backend
    backend = module.params['select_crypto_backend']
    if backend == 'auto':
        pass
    elif backend == 'openssl':
        HAS_CURRENT_CRYPTOGRAPHY = False
    elif backend == 'cryptography':
        try:
            cryptography.__version__
        except Exception as _:
            module.fail_json(msg='Cannot find cryptography module!')
        HAS_CURRENT_CRYPTOGRAPHY = True
    else:
        module.fail_json(msg='Unknown crypto backend "{0}"!'.format(backend))
    # Inform about choices
    if HAS_CURRENT_CRYPTOGRAPHY:
        module.debug('Using cryptography backend (library version {0})'.format(CRYPTOGRAPHY_VERSION))
    else:
        module.debug('Using OpenSSL binary backend')
