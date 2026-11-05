"""CSOPESY MO1 - Process Multiplexer and CLI.

Entry point: main() in this file (run `python csopesy.py`).
See README.txt for details.
"""
import os
import random
import sys
import threading
import time
from collections import deque

# ---------------------------------------------------------------------------
# Emulator constants (these are the "emulator changes" some quiz items ask for)
# ---------------------------------------------------------------------------
TICKS_PER_SECOND = 100      # real-time speed of the CPU tick counter
GEN_CYCLE_TICKS = 10        # one "batch-process-freq" unit, in CPU ticks
ALTERNATING_MODE = False    # True => PRINT("Value from: "+x) / ADD(x,x,[1-10]) alternating
UINT16_MAX = 65535
MAX_FOR_DEPTH = 3
MAX_LOGS = 1000
SMI_LOG_LINES = 50
LIVE_FEED_SECONDS = 1.0     # how often an attached process screen shows new output
LOG_FILE = "csopesy-log.txt"
UINT32_MAX = 2 ** 32

BANNER = r"""
  ____ ____   ___  ____  _____ ______   __
 / ___/ ___| / _ \|  _ \| ____/ ___\ \ / /
| |   \___ \| | | | |_) |  _| \___ \\ V /
| |___ ___) | |_| |  __/| |___ ___) || |
 \____|____/ \___/|_|   |_____|____/ |_|
"""


def stamp():
    return time.strftime("%m/%d/%Y %I:%M:%S%p")


def clamp16(v):
    return max(0, min(UINT16_MAX, v))


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class Config:
    def __init__(self):
        self.num_cpu = 4
        self.scheduler = "fcfs"
        self.quantum = 5
        self.batch_freq = 1
        self.min_ins = 1000
        self.max_ins = 2000
        self.delay = 0
        self.gen_cycle = GEN_CYCLE_TICKS
        self.alternating = ALTERNATING_MODE


def find_config():
    for base in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        p = os.path.join(base, "config.txt")
        if os.path.isfile(p):
            return p
    return None


def load_config():
    """Returns (Config, None) or (None, error message)."""
    path = find_config()
    if not path:
        return None, "config.txt not found."
    raw = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            parts = line.strip().split(None, 1)
            if len(parts) == 2:
                raw[parts[0].lower()] = parts[1].strip().strip('"').strip("'").strip()
    aliases = {"delays-per-exec": "delay-per-exec"}
    for a, b in aliases.items():
        if a in raw and b not in raw:
            raw[b] = raw[a]
    required = ["num-cpu", "scheduler", "quantum-cycles", "batch-process-freq",
                "min-ins", "max-ins", "delay-per-exec"]
    for k in required:
        if k not in raw:
            return None, "config.txt is missing parameter '%s'." % k

    def num(key, lo, hi):
        try:
            v = int(raw[key])
        except ValueError:
            raise ValueError("'%s' must be an integer." % key)
        if not lo <= v <= hi:
            raise ValueError("'%s' must be in range [%d, %d]." % (key, lo, hi))
        return v

    c = Config()
    try:
        c.num_cpu = num("num-cpu", 1, 128)
        c.scheduler = raw["scheduler"].lower()
        if c.scheduler not in ("fcfs", "rr"):
            raise ValueError("'scheduler' must be \"fcfs\" or \"rr\".")
        c.quantum = num("quantum-cycles", 1, UINT32_MAX)
        c.batch_freq = num("batch-process-freq", 1, UINT32_MAX)
        c.min_ins = num("min-ins", 1, UINT32_MAX)
        c.max_ins = num("max-ins", 1, UINT32_MAX)
        c.delay = num("delay-per-exec", 0, UINT32_MAX)
        if c.min_ins > c.max_ins:
            raise ValueError("'min-ins' must not exceed 'max-ins'.")
        # optional, not part of the base spec
        if "gen-cycle-ticks" in raw:
            c.gen_cycle = num("gen-cycle-ticks", 1, UINT32_MAX)
        if "instruction-mode" in raw:
            m = raw["instruction-mode"].lower()
            if m not in ("random", "alternating"):
                raise ValueError("'instruction-mode' must be random or alternating.")
            c.alternating = (m == "alternating")
    except ValueError as e:
        return None, "Invalid config.txt: %s" % e
    return c, None


# ---------------------------------------------------------------------------
# Instruction generation
#   Instruction tuples:
#     ("PRINT", prefix, var|None)   ("DECLARE", var, value)
#     ("ADD", dst, a, b)  ("SUBTRACT", dst, a, b)   (a/b: variable name or int)
#     ("SLEEP", ticks)
#   FOR([body], repeats) is expanded lazily while the generator is consumed,
#   every executed instruction counts as one line of code. Nesting <= 3.
# ---------------------------------------------------------------------------
VAR_POOL = ["a", "b", "c", "d", "x", "y", "z"]


def _simple(rng, name):
    kind = rng.choices(["PRINT", "DECLARE", "ADD", "SUBTRACT", "SLEEP"],
                       weights=[4, 2, 3, 3, 1])[0]
    if kind == "PRINT":
        return ("PRINT", "Hello world from %s!" % name, None)
    if kind == "DECLARE":
        return ("DECLARE", rng.choice(VAR_POOL), rng.randint(0, 1000))
    if kind == "SLEEP":
        return ("SLEEP", rng.randint(1, 10))

    def operand():
        return rng.choice(VAR_POOL) if rng.random() < 0.5 else rng.randint(0, 100)
    return (kind, rng.choice(VAR_POOL), operand(), operand())


def _block(budget, depth, rng, name):
    while budget > 0:
        if depth < MAX_FOR_DEPTH and budget >= 2 and rng.random() < 0.08:
            body_len = rng.randint(1, min(5, budget // 2))
            repeats = rng.randint(2, max(2, min(5, budget // body_len)))
            body = list(_block(body_len, depth + 1, rng, name))
            for _ in range(repeats):
                yield from body
            budget -= body_len * repeats
        else:
            yield _simple(rng, name)
            budget -= 1


def make_program(total, name, rng, alternating):
    if alternating:
        def alt():
            for i in range(total):
                if i % 2 == 0:
                    yield ("PRINT", "Value from: ", "x")
                else:
                    yield ("ADD", "x", "x", rng.randint(1, 10))
        return alt()
    return _block(total, 0, rng, name)


# ---------------------------------------------------------------------------
# Process
# ---------------------------------------------------------------------------
class Process:
    def __init__(self, pid, name, total, alternating, rng):
        self.pid = pid
        self.name = name
        self.total = total
        self.executed = 0
        self.vars = {"x": 0} if alternating else {}
        self.program = make_program(total, name, rng, alternating)
        self.logs = deque(maxlen=MAX_LOGS)
        self.created = stamp()
        self.finished_at = None
        self.core = None
        self.finished = False
        self.delay_left = 0
        self.quantum_used = 0
        self.wake_tick = 0

    def _val(self, o):
        if isinstance(o, int):
            return clamp16(o)
        return self.vars.setdefault(o, 0)

    def execute(self, core):
        """Executes the next instruction. Returns the instruction tuple."""
        ins = next(self.program)
        op = ins[0]
        if op == "PRINT":
            text = ins[1]
            if ins[2] is not None:
                text += str(self._val(ins[2]))
            self.logs.append("(%s) Core:%d \"%s\"" % (stamp(), core, text))
        elif op == "DECLARE":
            self.vars[ins[1]] = clamp16(ins[2])
        elif op == "ADD":
            self.vars[ins[1]] = clamp16(self._val(ins[2]) + self._val(ins[3]))
        elif op == "SUBTRACT":
            self.vars[ins[1]] = clamp16(self._val(ins[2]) - self._val(ins[3]))
        self.executed += 1
        return ins


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
class Scheduler:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.rng = random.Random()
        self.tick = 0
        self.cores = [None] * cfg.num_cpu
        self.ready = deque()
        self.sleeping = []
        self.processes = {}      # name -> Process (all, incl. finished)
        self.finished = []
        self.next_pid = 1
        self.auto_counter = 0
        self.generating = False
        self.stop_flag = False
        self.gen_period = cfg.batch_freq * cfg.gen_cycle

    # -- process creation --
    def create(self, name):
        total = self.rng.randint(self.cfg.min_ins, self.cfg.max_ins)
        p = Process(self.next_pid, name, total, self.cfg.alternating, self.rng)
        self.next_pid += 1
        self.processes[name] = p
        self.ready.append(p)
        self._assign()
        return p

    def auto_name(self):
        while True:
            self.auto_counter += 1
            n = "p%02d" % self.auto_counter
            if n not in self.processes:
                return n

    # -- one CPU tick --
    def step(self):
        with self.lock:
            self.tick += 1
            if self.sleeping:
                still = []
                for p in self.sleeping:
                    if self.tick >= p.wake_tick:
                        self.ready.append(p)
                    else:
                        still.append(p)
                self.sleeping = still
            if self.generating and self.tick % self.gen_period == 0:
                self.create(self.auto_name())
            self._assign()
            rr = self.cfg.scheduler == "rr"
            for i, p in enumerate(self.cores):
                if p is None:
                    continue
                p.quantum_used += 1
                if p.delay_left > 0:
                    p.delay_left -= 1
                else:
                    ins = p.execute(i)
                    if p.executed >= p.total:
                        p.finished = True
                        p.finished_at = stamp()
                        p.core = None
                        self.cores[i] = None
                        self.finished.append(p)
                        continue
                    p.delay_left = self.cfg.delay
                    if ins[0] == "SLEEP":
                        p.wake_tick = self.tick + ins[1] + 1
                        p.core = None
                        p.quantum_used = 0
                        self.cores[i] = None
                        if ins[1] > 0:
                            self.sleeping.append(p)
                        else:
                            self.ready.append(p)
                        continue
                if rr and p.quantum_used >= self.cfg.quantum:
                    p.quantum_used = 0
                    if self.ready:
                        p.core = None
                        self.cores[i] = None
                        self.ready.append(p)
            self._assign()

    def _assign(self):
        for i in range(len(self.cores)):
            if not self.ready:
                return
            if self.cores[i] is None:
                p = self.ready.popleft()
                p.core = i
                p.quantum_used = 0
                self.cores[i] = p

    # -- reporting --
    def report(self):
        with self.lock:
            used = sum(1 for c in self.cores if c is not None)
            total = len(self.cores)
            util = int(round(100.0 * used / total))
            lines = ["CPU utilization: %d%%" % util,
                     "Cores used: %d" % used,
                     "Cores available: %d" % (total - used),
                     "",
                     "--------------------------------------",
                     "Running processes:"]
            for i, p in enumerate(self.cores):
                if p is not None:
                    lines.append("%-12s (%s)   Core: %d   %d / %d" %
                                 (p.name, p.created, i, p.executed, p.total))
            waiting = len(self.ready) + len(self.sleeping)
            if waiting:
                lines.append("(%d more process(es) waiting for a core)" % waiting)
            lines += ["", "Finished processes:"]
            for p in self.finished:
                lines.append("%-12s (%s)   Finished   %d / %d" %
                             (p.name, p.finished_at, p.total, p.total))
            lines.append("--------------------------------------")
            return "\n".join(lines)

    def find_live(self, name):
        with self.lock:
            p = self.processes.get(name)
            return p if p and not p.finished else None

    def smi(self, p):
        with self.lock:
            lines = ["Process name: %s" % p.name, "ID: %d" % p.pid, "Logs:"]
            logs = list(p.logs)[-SMI_LOG_LINES:]
            lines += logs if logs else ["(no output yet)"]
            lines.append("")
            if p.finished:
                lines.append("Finished!")
            else:
                lines.append("Current instruction line: %d" % p.executed)
                lines.append("Lines of code: %d" % p.total)
            return "\n".join(lines)

    # -- clock thread --
    def run_clock(self):
        period = 1.0 / TICKS_PER_SECOND
        nxt = time.perf_counter()
        while not self.stop_flag:
            now = time.perf_counter()
            if now >= nxt:
                self.step()
                nxt += period
                if now - nxt > 0.25:     # fell behind; don't spiral
                    nxt = now
            else:
                time.sleep(min(nxt - now, 0.002))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def clear():
    os.system("cls" if os.name == "nt" else "clear")


def print_header():
    clear()
    print(BANNER)
    print("Welcome to CSOPESY Emulator!")
    print("Type 'initialize' to begin, 'exit' to quit.\n")


def live_feed(sched, p, stop):
    """Prints the process's newest PRINT output about once a second while attached."""
    prompt = "[%s] root:\\> " % p.name
    last = p.logs[-1] if p.logs else None
    while not stop.wait(LIVE_FEED_SECONDS):
        with sched.lock:
            newest = p.logs[-1] if p.logs else None
            done = p.finished
        if newest is not None and newest != last:
            last = newest
            print("\n" + newest + "\n" + prompt, end="", flush=True)
        if done:
            print("\nFinished!\n" + prompt, end="", flush=True)
            return


def process_screen(sched, p):
    clear()
    print(sched.smi(p))
    print()
    stop = threading.Event()
    threading.Thread(target=live_feed, args=(sched, p, stop), daemon=True).start()
    try:
        _process_loop(sched, p)
    finally:
        stop.set()


def _process_loop(sched, p):
    while True:
        try:
            line = input("[%s] root:\\> " % p.name).strip()
        except (EOFError, KeyboardInterrupt):
            return
        cmd = line.lower()
        if cmd == "process-smi":
            print(sched.smi(p))
        elif cmd == "exit":
            clear()
            return
        elif cmd == "":
            continue
        elif cmd in ("screen -ls", "screen-ls"):
            print(sched.report())
        elif cmd.split()[:2] == ["screen", "-r"] and line.split()[2:3] == [p.name]:
            clear()
            print(sched.smi(p))
        else:
            print("Unknown command. Available: process-smi, screen -ls, screen -r %s, exit" % p.name)


def cmd_screen(sched, args):
    if not args:
        print("Usage: screen -s <name> | screen -r <name> | screen -ls")
        return
    flag = args[0].lower()
    if flag == "-ls":
        print(sched.report())
    elif flag in ("-s", "-r"):
        if len(args) < 2:
            print("Usage: screen %s <process name>" % flag)
            return
        name = args[1]
        if flag == "-r":
            p = sched.find_live(name)
            if p is None:
                print("Process %s not found." % name)
                return
        else:
            with sched.lock:
                if name in sched.processes:
                    print("Process %s already exists." % name)
                    return
                sched.create(name)
            print("Process %s created. Use 'screen -r %s' to view it." % (name, name))
            return
        process_screen(sched, p)
    else:
        print("Unknown screen option '%s'." % args[0])


def cmd_report(sched):
    text = sched.report()
    print(text)
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), LOG_FILE)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print("\nReport saved to %s" % path)
    except OSError as e:
        print("Could not write %s: %s" % (LOG_FILE, e))


def main():
    sched = None
    print_header()
    try:
        while True:
            try:
                line = input("root:\\> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not line:
                continue
            parts = line.split()
            cmd, args = parts[0].lower(), parts[1:]
            if cmd == "report" and args and args[0].lower() == "-util":
                cmd, args = "report-util", []
            if cmd == "exit":
                break
            if sched is None:
                if cmd == "initialize":
                    cfg, err = load_config()
                    if err:
                        print(err)
                        continue
                    sched = Scheduler(cfg)
                    threading.Thread(target=sched.run_clock, daemon=True).start()
                    print("Initialized: %d CPU(s), scheduler=%s, quantum-cycles=%d, "
                          "batch-process-freq=%d, min-ins=%d, max-ins=%d, delay-per-exec=%d"
                          % (cfg.num_cpu, cfg.scheduler, cfg.quantum, cfg.batch_freq,
                             cfg.min_ins, cfg.max_ins, cfg.delay))
                else:
                    print("Please run 'initialize' first.")
                continue
            if cmd == "initialize":
                print("Already initialized.")
            elif cmd == "screen":
                cmd_screen(sched, args)
            elif cmd == "screen-ls":
                print(sched.report())
            elif cmd in ("scheduler-start", "scheduler-test"):
                if sched.generating:
                    print("Scheduler is already generating processes.")
                else:
                    sched.generating = True
                    print("Process generation started.")
            elif cmd == "scheduler-stop":
                if not sched.generating:
                    print("Scheduler is not generating processes.")
                else:
                    sched.generating = False
                    print("Process generation stopped.")
            elif cmd == "report-util":
                cmd_report(sched)
            else:
                print("Unknown command '%s'." % cmd)
    finally:
        if sched:
            sched.stop_flag = True


if __name__ == "__main__":
    main()
