#!/usr/bin/python3
"""Read-only, 20-second uprobes for the exact audited Ubuntu Mutter binary.
Requires root/BCC. Does not modify code, variables, settings, services or pixels.
Offsets/registers come from matching Ubuntu debug symbols and disassembly.
"""
import argparse
import hashlib
import json
from pathlib import Path
import time

LIBRARY = Path('/usr/lib/x86_64-linux-gnu/libmutter-14.so.0')
SHA256 = '03fad88986cff172f3b9ddd6810a5523c220f4cdc426b429a9973e7286332530'

PROGRAM = r'''
#include <uapi/linux/ptrace.h>
struct key { u32 node; u32 phase; u32 kind; s64 value; };
BPF_HASH(counters, struct key, u64, 4096);
static __always_inline struct key location(struct pt_regs *ctx, u32 kind) {
    struct key key = {};
    bpf_probe_read_user(&key.node, 4, (void *)(ctx->bx + 0x90));
    bpf_probe_read_user(&key.phase, 4, (void *)(ctx->bp - 0x90));
    key.kind = kind;
    return key;
}
int rate(struct pt_regs *ctx) {
    if (bpf_get_current_pid_tgid() >> 32 != TARGET_PID) return 0;
    struct key key = location(ctx, 0);
    counters.increment(key);
    s64 minimum = ctx->ax, gap = ctx->dx;
    key.kind = 5; key.value = minimum;
    counters.increment(key);
    key.value = 0;
    if (gap < minimum) {
        key.kind = 1;
        s64 deficit = minimum - gap;
        key.value = deficit < 100 ? deficit : deficit < 1000 ? deficit / 100 * 100 : deficit / 1000 * 1000;
    } else { key.kind = 2; }
    counters.increment(key);
    return 0;
}
int no_buffer(struct pt_regs *ctx) {
    if (bpf_get_current_pid_tgid() >> 32 != TARGET_PID) return 0;
    struct key key = location(ctx, 3);
    counters.increment(key);
    return 0;
}
int not_ready(struct pt_regs *ctx) {
    if (bpf_get_current_pid_tgid() >> 32 != TARGET_PID) return 0;
    struct key key = location(ctx, 4);
    counters.increment(key);
    return 0;
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('pid', type=int)
    args = parser.parse_args()
    if args.pid <= 1 or Path(f'/proc/{args.pid}/comm').read_text().strip() != 'gnome-shell':
        parser.error('Expected a live gnome-shell PID')
    if hashlib.sha256(LIBRARY.read_bytes()).hexdigest() != SHA256:
        parser.error('Unreviewed Mutter binary; offsets MUST NOT be reused')
    from bcc import BPF
    trace = BPF(text=PROGRAM.replace('TARGET_PID', str(args.pid)))
    try:
        for offset, function in [(0x1406c6, 'rate'), (0x1408d0, 'no_buffer'), (0x14077f, 'not_ready')]:
            trace.attach_uprobe(name=str(LIBRARY), addr=offset, fn_name=function, pid=args.pid)
        print('Pacing counters ready; auto-detach after 20 seconds', flush=True)
        time.sleep(20)
        kinds = ['rate_check', 'too_early', 'passed_rate_gate', 'no_free_buffer', 'buffers_not_ready', 'minimum_us']
        records = [dict(node=key.node, phase=key.phase, event=kinds[key.kind], value=key.value, count=value.value)
                   for key, value in trace['counters'].items()]
        print(json.dumps(dict(binary_sha256=SHA256, seconds=20,
            records=sorted(records, key=lambda x: (x['node'], x['phase'], x['event'], x['value']))), indent=2))
    finally:
        trace.cleanup()


if __name__ == '__main__':
    main()
