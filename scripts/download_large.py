#!/usr/bin/env python3
"""
Fast parallel resumable downloader for large files using HTTP Range requests.

Speed upgrades vs "parts + merge":
- Writes directly into the final output file at the correct offsets (no merge pass).
- Uses larger chunk sizes by default (8 MiB).
- Uses per-thread sessions with tuned HTTPAdapter pool sizes.
- Resume state tracked via tiny .state files per part (safe even if slightly stale).

Usage:
  python fast_download.py --url URL --out FILE
  python fast_download.py --url URL --out FILE --parts 32 --chunk-mb 8

Notes:
- Requires: requests
- Best performance typically with --parts 16..64 depending on server/network.
"""

import argparse
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from threading import Lock, local

import requests
from requests.adapters import HTTPAdapter


_tls = local()


def _make_session(pool_size: int) -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "User-Agent": "fast-downloader/2.0",
            "Accept-Encoding": "identity",  # avoid gzip/deflate; we want raw bytes for Range
        }
    )
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


def get_thread_session(pool_size: int) -> requests.Session:
    # One session per thread (requests.Session is not guaranteed thread-safe)
    if not hasattr(_tls, "session"):
        _tls.session = _make_session(pool_size)
    return _tls.session


def probe_size_and_range(url: str, timeout=30, pool_size: int = 64):
    """
    Returns (size, range_ok). Uses:
    - HEAD for Content-Length if available
    - GET Range: bytes=0-0 to verify 206 + Content-Range (real range support)
    """
    sess = get_thread_session(pool_size)

    size = 0
    try:
        r = sess.head(url, allow_redirects=True, timeout=timeout)
        r.raise_for_status()
        size = int(r.headers.get("Content-Length") or 0)
    except Exception:
        pass

    range_ok = False
    try:
        r = sess.get(
            url,
            headers={"Range": "bytes=0-0"},
            stream=True,
            timeout=timeout,
            allow_redirects=True,
        )
        range_ok = (r.status_code == 206 and "Content-Range" in r.headers)
        if size == 0 and "Content-Range" in r.headers:
            # Content-Range: bytes 0-0/12345
            cr = r.headers["Content-Range"]
            if "/" in cr:
                total = cr.split("/")[-1].strip()
                if total.isdigit():
                    size = int(total)
    except Exception:
        range_ok = False

    return size, range_ok


def single_thread_download(url: str, out: str, chunk_bytes: int, timeout=(10, 120), pool_size: int = 16) -> bool:
    sess = get_thread_session(pool_size)
    print("Starting single-threaded download...")
    start_time = time.time()
    done = 0

    with sess.get(url, stream=True, timeout=timeout, allow_redirects=True) as r:
        r.raise_for_status()
        with open(out, "wb") as f:
            for chunk in r.iter_content(chunk_size=chunk_bytes):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    elapsed = max(1e-6, time.time() - start_time)
                    speed = done / elapsed
                    sys.stdout.write(f"\r{done} bytes at {speed/1024/1024:.2f} MB/s")
                    sys.stdout.flush()
    print()
    return True


def _read_state(state_path: str) -> int:
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            v = f.read().strip()
            return int(v) if v else 0
    except Exception:
        return 0


def _write_state(state_path: str, value: int):
    # Atomic-ish update: write temp then replace
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(str(value))
    os.replace(tmp, state_path)


def _write_at(fd: int, offset: int, data: bytes):
    # Use pwrite if available (avoids lseek); otherwise safe with per-thread fd + lseek/write
    pwrite = getattr(os, "pwrite", None)
    if pwrite is not None:
        pwrite(fd, data, offset)
    else:
        os.lseek(fd, offset, os.SEEK_SET)
        os.write(fd, data)


def download_part_direct(
    url: str,
    out_path: str,
    start: int,
    end: int,
    state_path: str,
    progress,
    lock: Lock,
    retries: int,
    backoff: float,
    chunk_bytes: int,
    pool_size: int,
    timeout=(10, 120),
):
    """
    Downloads range [start, end] directly into out_path at the proper offsets.
    Resume is tracked by state_path (bytes already written for this part).
    If state is stale, we may re-download some bytes, but we overwrite at offset => safe.
    """
    expected = end - start + 1

    # Each worker uses its own file descriptor (safe concurrent writes)
    fd = os.open(out_path, os.O_WRONLY)

    try:
        attempt = 0
        while attempt <= retries:
            done_bytes = _read_state(state_path)
            if done_bytes >= expected:
                return True, "ok"

            range_start = start + done_bytes
            headers = {"Range": f"bytes={range_start}-{end}"}

            try:
                sess = get_thread_session(pool_size)
                with sess.get(url, headers=headers, stream=True, timeout=timeout, allow_redirects=True) as r:
                    # Must honor Range for parallel mode
                    if r.status_code != 206:
                        return False, f"range_not_honored_status_{r.status_code}"
                    if "Content-Range" not in r.headers:
                        return False, "missing_content_range"
                    r.raise_for_status()

                    # Update state occasionally (not every chunk write to avoid overhead)
                    last_state_flush = time.time()
                    local_done = done_bytes
                    write_offset = start + local_done

                    for chunk in r.iter_content(chunk_size=chunk_bytes):
                        if not chunk:
                            continue

                        _write_at(fd, write_offset, chunk)
                        write_offset += len(chunk)
                        local_done += len(chunk)

                        with lock:
                            progress[0] += len(chunk)

                        # Flush state at most ~4x/sec
                        now = time.time()
                        if now - last_state_flush >= 0.25:
                            _write_state(state_path, local_done)
                            last_state_flush = now

                    # Final flush
                    _write_state(state_path, local_done)

                # Verify complete
                if _read_state(state_path) >= expected:
                    return True, "ok"

                # If server ended early, retry
                raise RuntimeError("short_read")

            except Exception as e:
                attempt += 1
                if attempt > retries:
                    return False, f"exception: {e}"
                time.sleep(backoff ** attempt)

        return False, "unknown"

    finally:
        os.close(fd)


def parallel_download(url: str, out: str, parts: int, retries: int, chunk_mb: int):
    chunk_bytes = max(1, chunk_mb) * 1024 * 1024

    # Pool size: keep up with parts (but don't go crazy)
    pool_size = min(max(16, parts), 128)

    size, range_ok = probe_size_and_range(url, pool_size=pool_size)

    if size <= 0:
        print("Could not determine remote size. Falling back to single-threaded.")
        return single_thread_download(url, out, chunk_bytes=chunk_bytes, pool_size=16)

    if not range_ok or parts <= 1:
        return single_thread_download(url, out, chunk_bytes=chunk_bytes, pool_size=16)

    # Create resume dir + output file pre-sized (important for random writes)
    state_dir = out + ".parts"
    os.makedirs(state_dir, exist_ok=True)

    # Pre-size output file (sparse-friendly on most filesystems)
    # If file exists with correct size, keep it; otherwise create/resize.
    if not os.path.exists(out):
        with open(out, "wb") as f:
            f.truncate(size)
    else:
        try:
            if os.path.getsize(out) != size:
                with open(out, "r+b") as f:
                    f.truncate(size)
        except Exception:
            # If we can't stat/resize, fallback to single-thread
            print("Could not size output file. Falling back to single-threaded.")
            return single_thread_download(url, out, chunk_bytes=chunk_bytes, pool_size=16)

    part_size = math.ceil(size / parts)
    tasks = []
    for i in range(parts):
        s = i * part_size
        e = min(s + part_size - 1, size - 1)
        state_path = os.path.join(state_dir, f"part-{i:04d}.state")
        tasks.append((s, e, state_path))

    # Initialize progress from state files
    progress = [0]
    lock = Lock()
    for (s, e, sp) in tasks:
        expected = e - s + 1
        done = min(_read_state(sp), expected)
        progress[0] += done

    print(f"Starting FAST parallel download: size={size} bytes, parts={parts}, chunk={chunk_mb} MiB")
    start_time = time.time()

    ok = True
    range_not_supported = False

    with ThreadPoolExecutor(max_workers=min(parts, 64)) as ex:
        future_map = {}
        for (s, e, sp) in tasks:
            fut = ex.submit(
                download_part_direct,
                url,
                out,
                s,
                e,
                sp,
                progress,
                lock,
                retries,
                1.5,  # backoff
                chunk_bytes,
                pool_size,
            )
            future_map[fut] = (s, e, sp)

        last_print = 0.0
        while future_map:
            done, _ = wait(future_map.keys(), timeout=0.5, return_when=FIRST_COMPLETED)

            now = time.time()
            if now - last_print >= 0.5:
                with lock:
                    done_bytes = progress[0]
                pct = done_bytes / size * 100.0
                speed = done_bytes / max(1e-6, now - start_time)
                sys.stdout.write(
                    f"\r{done_bytes}/{size} bytes ({pct:.2f}%) at {speed/1024/1024:.2f} MB/s"
                )
                sys.stdout.flush()
                last_print = now

            for fut in done:
                s, e, sp = future_map.pop(fut)
                try:
                    success, msg = fut.result()
                    if not success:
                        print(f"\nPart failed ({s}-{e}): {msg}")
                        ok = False
                        if msg.startswith("range_not_honored_status_"):
                            range_not_supported = True
                except Exception as exc:
                    print(f"\nPart raised exception ({s}-{e}): {exc}")
                    ok = False

    # Final line
    with lock:
        done_bytes = progress[0]
    elapsed = max(1e-6, time.time() - start_time)
    speed = done_bytes / elapsed
    pct = done_bytes / size * 100.0
    print(f"\r{done_bytes}/{size} bytes ({pct:.2f}%) at {speed/1024/1024:.2f} MB/s")

    if range_not_supported:
        print("Server did not honor Range. Falling back to single-threaded.")
        return single_thread_download(url, out, chunk_bytes=chunk_bytes, pool_size=16)

    if not ok:
        print("One or more parts failed. Re-run to resume.")
        return False

    # Verify all parts complete
    for (s, e, sp) in tasks:
        expected = e - s + 1
        done = _read_state(sp)
        if done < expected:
            print(f"Part incomplete ({s}-{e}): {done}/{expected}. Re-run to resume.")
            return False

    # Verify final size
    try:
        final_size = os.path.getsize(out)
        if final_size != size:
            print(f"Final file size mismatch: got {final_size}, expected {size}.")
            return False
    except Exception:
        pass

    # Optional cleanup of state files (comment out if you like keeping them)
    try:
        for _, _, sp in tasks:
            os.remove(sp)
        os.rmdir(state_dir)
    except Exception:
        pass

    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True, help="URL of the large file to download")
    p.add_argument("--out", required=True, help="Output file path")
    p.add_argument("--parts", type=int, default=32, help="Number of parallel ranges (try 16-64)")
    p.add_argument("--retries", type=int, default=5, help="Retries per part")
    p.add_argument("--chunk-mb", type=int, default=8, help="Chunk size per read in MiB (4-16 often good)")
    args = p.parse_args()

    success = parallel_download(args.url, args.out, parts=args.parts, retries=args.retries, chunk_mb=args.chunk_mb)
    if not success:
        print("Download incomplete or failed")
        sys.exit(2)
    print("Download completed successfully")


if __name__ == "__main__":
    main()