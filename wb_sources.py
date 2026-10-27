"""Independent capability configuration and credential-bound HTTP requests."""
import os
from pathlib import Path
import re
import stat
import urllib.parse
import urllib.request

KINDS = ('chat', 'billing', 'models')
REALMS = ('intl', 'cn')
DEFAULTS = {'enabled': False, 'base_url': '', 'auth': 'none',
            'header_name': 'X-API-Key', 'key_file': '', 'timeout_seconds': 120}
FORBIDDEN_HEADERS = {'host', 'connection', 'content-length', 'content-type',
                     'transfer-encoding', 'upgrade', 'proxy-authorization',
                     'cookie', 'x-refresh-token', 'x-device-token'}


class SourceError(ValueError):
    """Only constant, credential-free diagnostics may leave this module."""


def origin(url):
    parsed = urllib.parse.urlsplit(url)
    return parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80)


def validate_base(value):
    if not isinstance(value, str) or len(value) > 2000 or re.search(r'[\x00-\x20\x7f]', value):
        raise SourceError('接口地址格式无效')
    try:
        parsed = urllib.parse.urlsplit(value)
        parsed.port
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1', 'localhost', '::1'))
                or '\\' in value or '{' in value or '}' in value):
            raise ValueError
    except ValueError:
        raise SourceError('接口需为本机 HTTP 或远程 HTTPS 地址，不含凭据、查询参数或片段') from None
    return value.rstrip('/')


def validate_capabilities(raw, complete=False):
    if not isinstance(raw, dict) or set(raw) - set(KINDS):
        raise SourceError('接口能力配置无效')
    result = {}
    for kind, realms in raw.items():
        if not isinstance(realms, dict) or set(realms) - set(REALMS):
            raise SourceError('接口区域配置无效')
        result[kind] = {}
        for realm, item in realms.items():
            if not isinstance(item, dict) or set(item) - set(DEFAULTS):
                raise SourceError('接口配置字段无效')
            clean = {}
            for key, value in item.items():
                if key == 'enabled':
                    if type(value) is not bool: raise SourceError('接口启用状态需为布尔值')
                elif key == 'timeout_seconds':
                    if type(value) is not int or not 1 <= value <= 300:
                        raise SourceError('接口超时需为 1 至 300 秒')
                else:
                    if not isinstance(value, str) or len(value) > 2000 or re.search(r'[\x00-\x1f\x7f]', value):
                        raise SourceError('接口配置文字无效')
                    value = value.strip()
                    if key == 'base_url' and value: value = validate_base(value)
                    if key == 'auth' and value not in ('none', 'bearer', 'header', 'account'):
                        raise SourceError('请选择有效的接口鉴权方式')
                    if key == 'key_file' and value and (not Path(value).is_absolute() or '..' in Path(value).parts):
                        raise SourceError('接口凭据文件需使用绝对路径')
                    if key == 'header_name' and value and (not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]{0,63}', value) or value.lower() in FORBIDDEN_HEADERS):
                        raise SourceError('接口鉴权请求头名称无效')
                clean[key] = value
            if complete:
                clean = {**DEFAULTS, **clean}
                if clean['enabled']:
                    if not clean['base_url']: raise SourceError('启用独立接口时必须填写地址')
                    if clean['auth'] in ('bearer', 'header') and not clean['key_file']:
                        raise SourceError('此接口鉴权方式需要独立凭据文件')
                    if clean['auth'] == 'header' and not clean['header_name']:
                        raise SourceError('请填写此接口的鉴权请求头名称')
            result[kind][realm] = clean
    return result


def config(raw, kind, realm):
    if kind not in KINDS or realm not in REALMS: raise SourceError('未知接口能力或区域')
    if not isinstance(raw, dict): raise SourceError('接口能力配置无效')
    realms = raw.get(kind, {})
    if not isinstance(realms, dict): raise SourceError('接口区域配置无效')
    return validate_capabilities({kind: {realm: realms.get(realm, {})}}, complete=True)[kind][realm]


def read_key(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > 8192:
                raise ValueError
            if os.name == 'posix' and (info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid())):
                raise ValueError
            value = stream.read(8193).decode('utf-8').strip()
        if not value or len(value) > 8192 or re.search(r'[\x00-\x1f\x7f]', value):
            raise ValueError
        return value
    except (OSError, UnicodeError, ValueError):
        raise SourceError('接口凭据文件不可用；请检查文件存在、所有者及仅所有者读写权限') from None


class SourceHeaders(dict):
    def __init__(self, values, base_url, timeout, kind):
        super().__init__(values)
        self.base_url = base_url
        self.timeout = timeout
        self.kind = kind


def resolve(source, kind, native_base, native_headers, allowed_origins=()):
    if not source['enabled']:
        return native_base, native_headers()
    base = source['base_url']
    if source['auth'] == 'account':
        if origin(base) not in {origin(url) for url in (native_base, *allowed_origins)}:
            raise SourceError('当前账号身份只可用于该区域的原有官方接口')
        headers = native_headers()
    else:
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json',
                   'User-Agent': 'WorkBuddy-Hub'}
        if source['auth'] != 'none':
            value = read_key(source['key_file'])
            headers['Authorization' if source['auth'] == 'bearer' else source['header_name']] = (
                'Bearer ' + value if source['auth'] == 'bearer' else value)
    return base, SourceHeaders(headers, base, source['timeout_seconds'], kind)


def request(url, data=None, headers=None, method=None):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    req.wb_exact_headers = list((headers or {}).items())
    if isinstance(headers, SourceHeaders):
        path = urllib.parse.urlsplit(url).path
        prefix = urllib.parse.urlsplit(headers.base_url).path.rstrip('/')
        if origin(url) != origin(headers.base_url) or (prefix and path != prefix and not path.startswith(prefix + '/')):
            raise SourceError('接口地址与凭据来源不一致，请重新读取配置')
        req.capability_source = True
        req.capability_timeout = headers.timeout
        req.capability_kind = headers.kind
    return req


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise SourceError('独立接口不允许重定向')
