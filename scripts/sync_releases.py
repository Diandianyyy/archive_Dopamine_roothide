"""Archive public upstream releases and a recoverable full Git source bundle."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

TOKEN = os.environ['GH_TOKEN']
UPSTREAM = os.environ['UPSTREAM_REPOSITORY']
TARGET = os.environ['GITHUB_REPOSITORY']
API = 'https://api.github.com'

def request(url, method='GET', data=None, binary=False):
    if url.startswith('/'):
        url = API + url
    headers = {'User-Agent': 'release-archive', 'Accept': 'application/vnd.github+json'}
    if urllib.parse.urlparse(url).hostname in ('api.github.com', 'uploads.github.com'):
        headers['Authorization'] = 'Bearer ' + TOKEN
    if data is not None:
        if binary:
            headers['Content-Type'] = 'application/octet-stream'
        else:
            data = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
    for attempt in range(5):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers, method=method), timeout=300) as r:
                payload = r.read()
                return payload if binary else (json.loads(payload) if payload else None)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == 4:
                raise
        except (OSError, TimeoutError):
            if attempt == 4:
                raise
        time.sleep(5 * 2 ** attempt)

def listing(path):
    result = []
    page = 1
    while True:
        items = request(f'{path}?per_page=100&page={page}')
        result.extend(items)
        if len(items) < 100:
            return result
        page += 1

def upload(release, name, payload):
    url = release['upload_url'].split('{')[0] + '?name=' + urllib.parse.quote(name, safe='')
    return request(url, method='POST', data=payload, binary=True)

def ensure_release(tag, name, body, prerelease=False):
    try:
        release = request(f'/repos/{TARGET}/releases/tags/{urllib.parse.quote(tag, safe="")}')
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        return request(f'/repos/{TARGET}/releases', 'POST', {
            'tag_name': tag, 'name': name, 'body': body, 'prerelease': prerelease,
            'target_commitish': 'main', 'make_latest': 'false'})
    if (release['name'], release['body'], release['prerelease']) != (name, body, prerelease):
        release = request(f'/repos/{TARGET}/releases/{release["id"]}', 'PATCH', {
            'name': name, 'body': body, 'prerelease': prerelease})
    return release

errors = []
copied = 0
releases = listing(f'/repos/{UPSTREAM}/releases')
for upstream in reversed(releases):
    tag = upstream['tag_name']
    try:
        metadata = {k: upstream[k] for k in ('id', 'tag_name', 'name', 'body', 'published_at', 'prerelease', 'html_url', 'assets')}
        body = f'Original release: {upstream["html_url"]}\n\n' + (upstream['body'] or '')
        release = ensure_release(tag, upstream['name'] or tag, body, upstream['prerelease'])
        assets = {a['name']: a for a in listing(f'/repos/{TARGET}/releases/{release["id"]}/assets')}
        for asset in upstream['assets']:
            try:
                name = asset['name']
                existing = assets.get(name)
                if existing and existing['size'] == asset['size'] and existing.get('state') == 'uploaded':
                    if not asset.get('digest') or existing.get('digest') == asset['digest']:
                        continue
                payload = request(asset['browser_download_url'], binary=True)
                if len(payload) != asset['size']:
                    raise ValueError('Downloaded size mismatch')
                digest = 'sha256:' + hashlib.sha256(payload).hexdigest()
                if asset.get('digest') and digest != asset['digest']:
                    raise ValueError('SHA256 mismatch')
                if name.lower().endswith(('.ipa', '.tipa', '.zip')):
                    import io
                    with zipfile.ZipFile(io.BytesIO(payload)) as z:
                        if z.testzip():
                            raise ValueError('Corrupt ZIP/IPA archive')
                if existing:
                    # Preserve replaced upstream files instead of silently losing history.
                    archived_name = f'history-{existing["id"]}-{name}'
                    request(f'/repos/{TARGET}/releases/assets/{existing["id"]}', 'PATCH', {'name': archived_name})
                upload(release, name, payload)
                copied += 1
                print(f'Archived {tag}/{name} ({len(payload)} bytes)', flush=True)
            except Exception as e:
                errors.append(f'{tag}/{asset["name"]}: {e}')
                print('SKIPPED ' + errors[-1], flush=True)
        record = json.dumps(metadata, ensure_ascii=False, indent=2).encode()
        old = assets.get('_upstream-release.json')
        if old:
            previous = request(old['browser_download_url'], binary=True)
            if previous == record:
                continue
            request(f'/repos/{TARGET}/releases/assets/{old["id"]}', 'DELETE')
        upload(release, '_upstream-release.json', record)
    except Exception as e:
        errors.append(f'{tag}: {e}')
        print('SKIPPED ' + errors[-1], flush=True)

try:
    # A Git bundle preserves all branches, tags and their complete reachable history.
    refs = subprocess.check_output(['git', 'ls-remote', f'https://github.com/{UPSTREAM}.git'], text=True)
    fingerprint = hashlib.sha256(refs.encode()).hexdigest()
    source = ensure_release('archive-source-history', 'Full upstream Git source history',
                            f'Full Git bundle for https://github.com/{UPSTREAM}\nRestore: git clone source.bundle\nRefs SHA256: {fingerprint}')
    name = f'source-{fingerprint}.bundle'
    assets = listing(f'/repos/{TARGET}/releases/{source["id"]}/assets')
    if not any(a['name'] == name and a.get('state') == 'uploaded' for a in assets):
        with tempfile.TemporaryDirectory() as tmp:
            repo = str(Path(tmp) / 'upstream.git')
            bundle = Path(tmp) / 'source.bundle'
            subprocess.run(['git', 'clone', '--mirror', f'https://github.com/{UPSTREAM}.git', repo], check=True)
            subprocess.run(['git', '-C', repo, 'bundle', 'create', str(bundle), '--all'], check=True)
            upload(source, name, bundle.read_bytes())
except Exception as e:
    errors.append(f'Git source bundle: {e}')

try:
    latest = request(f'/repos/{UPSTREAM}/releases/latest')
    mirrored = request(f'/repos/{TARGET}/releases/tags/{urllib.parse.quote(latest["tag_name"], safe="")}')
    request(f'/repos/{TARGET}/releases/{mirrored["id"]}', 'PATCH', {'make_latest': 'true'})
except Exception as e:
    errors.append(f'Latest release marker: {e}')
summary = f'Upstream: {UPSTREAM}\nReleases checked: {len(releases)}\nNew assets archived: {copied}\nFailures: {len(errors)}\n' + '\n'.join(errors)
print(summary, flush=True)
Path('sync-report.txt').write_text(summary, encoding='utf-8')
if os.environ.get('GITHUB_STEP_SUMMARY'):
    with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
        f.write(summary)
# Fail visibly after processing everything, so a partial backup cannot look complete.
if errors:
    raise SystemExit(1)
