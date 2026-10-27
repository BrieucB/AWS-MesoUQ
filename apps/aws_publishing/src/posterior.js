// Release v1 publishes figures/posterior.png as this exact flattened name.
const NAME = 'figures__posterior.png';
const MAX_BYTES = 10 * 1024 * 1024;
const PNG = [137, 80, 78, 71, 13, 10, 26, 10];
class PosteriorError extends Error {}
function validate(artifact) {
  if (!artifact || artifact.name !== NAME || artifact.kind !== 'result' ||
      artifact.media_type !== 'image/png' || typeof artifact.id !== 'string' ||
      !artifact.id.length || artifact.id.length > 256 || /[\u0000-\u001f\u007f]/.test(artifact.id) ||
      typeof artifact.sha256 !== 'string' || !/^[a-f0-9]{64}$/.test(artifact.sha256) ||
      !Number.isSafeInteger(artifact.size_bytes) || artifact.size_bytes < PNG.length || artifact.size_bytes > MAX_BYTES) {
    throw new PosteriorError('Posterior image metadata is invalid or exceeds the 10 MiB limit.');
  }
  return Object.freeze(Object.fromEntries(['id', 'name', 'kind', 'media_type', 'sha256', 'size_bytes'].map(key => [key, artifact[key]])));
}
export function posteriorArtifact(result) {
  if (result?.artifacts === undefined) return null;
  if (!Array.isArray(result.artifacts)) throw new PosteriorError('Posterior artifact list is invalid.');
  const candidates = result.artifacts.filter(artifact => artifact?.name === NAME);
  if (candidates.length > 1) throw new PosteriorError('Posterior image references are ambiguous.');
  return candidates.length ? validate(candidates[0]) : null;
}
function checkAbort(signal) {
  if (signal?.aborted) throw new DOMException('Posterior image loading was cancelled.', 'AbortError');
}
async function abortable(promise, signal) {
  checkAbort(signal);
  if (!signal) return promise;
  let listener;
  const aborted = new Promise((_, reject) => {
    listener = () => reject(new DOMException('Posterior image loading was cancelled.', 'AbortError'));
    signal.addEventListener('abort', listener, {once:true});
  });
  try { return await Promise.race([promise, aborted]); }
  finally { signal.removeEventListener('abort', listener); }
}
export async function loadPosteriorImage({api, project, artifact, fetcher = fetch, signal}) {
  let reader;
  try {
    checkAbort(signal);
    const trusted = validate(artifact);
    const response = await abortable(api.request(api.project(project, '/artifacts/' + encodeURIComponent(trusted.id))), signal);
    const returned = validate(response?.artifact);
    if (Object.keys(trusted).some(key => trusted[key] !== returned[key])) throw new PosteriorError('Posterior image metadata differs from the completed result.');
    api.validateStorageUrl(response.download_url);
    checkAbort(signal);
    const download = await abortable(fetcher.call(globalThis, response.download_url, {method:'GET', credentials:'omit', redirect:'error', cache:'no-store', signal}), signal);
    reader = download.body?.getReader?.();
    if (!download.ok || download.headers.get('content-type') !== 'image/png') throw new PosteriorError('Posterior download is not a PNG image.');
    const length = download.headers.get('content-length');
    if (length !== null && (!/^[0-9]+$/.test(length) || Number(length) !== trusted.size_bytes)) throw new PosteriorError('Posterior image byte length does not match.');
    if (!reader) throw new PosteriorError('Posterior image download cannot be verified.');
    const bytes = new Uint8Array(trusted.size_bytes);
    let offset = 0;
    for (;;) {
      const {done, value} = await abortable(reader.read(), signal);
      if (done) break;
      if (!(value instanceof Uint8Array) || value.length > bytes.length - offset) throw new PosteriorError('Posterior image byte length does not match.');
      bytes.set(value, offset); offset += value.length;
    }
    if (offset !== trusted.size_bytes) throw new PosteriorError('Posterior image byte length does not match.');
    if (PNG.some((value, index) => bytes[index] !== value)) throw new PosteriorError('Posterior image PNG signature is invalid.');
    const sha256 = Array.from(new Uint8Array(await abortable(crypto.subtle.digest('SHA-256', bytes), signal)), value => value.toString(16).padStart(2, '0')).join('');
    if (sha256 !== trusted.sha256) throw new PosteriorError('Posterior image checksum does not match.');
    checkAbort(signal);
    const chunks = [];
    for (let index = 0; index < bytes.length; index += 32768) chunks.push(String.fromCharCode(...bytes.subarray(index, index + 32768)));
    return 'data:image/png;base64,' + btoa(chunks.join(''));
  } catch (error) {
    if (signal?.aborted) throw new DOMException('Posterior image loading was cancelled.', 'AbortError');
    if (error instanceof PosteriorError) throw error;
    throw new PosteriorError('Posterior image could not be loaded or verified.');
  } finally {
    if (reader) { try { await reader.cancel(); } catch {} }
  }
}
