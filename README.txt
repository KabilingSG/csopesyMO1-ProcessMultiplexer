CSOPESY MO1 - Process Multiplexer and CLI
Group 5 - Kabiling, Simon Gabriel

Run:    python csopesy.py        (Python 3.8+, no dependencies)
Entry:  csopesy.py -> main()
Config: config.txt (read by the "initialize" command)

Commands: initialize, exit, screen -s <name>, screen -r <name>, screen -ls (or screen-ls),
          scheduler-start (alias scheduler-test), scheduler-stop, report-util (writes csopesy-log.txt)
Inside a process screen: process-smi, exit

Emulator notes
- 1 CPU tick = 1/100 s (TICKS_PER_SECOND). Each core runs one instruction per tick
  (+ delay-per-exec busy-wait ticks). RR preempts after quantum-cycles ticks.
- batch-process-freq is counted in "generator cycles" of GEN_CYCLE_TICKS (=10) ticks.
  Test 6 (8 cores, 100 instr, freq 1) cannot finish within the 30s wait otherwise.
  Override with optional config key:  gen-cycle-ticks 1
- Quiz item 5 (alternating PRINT("Value from: "+x) / ADD(x,x,[1-10]), x=0):
  set ALTERNATING_MODE = True in csopesy.py, OR add to config.txt:  instruction-mode alternating
- screen-ls lists running (on-core) processes, a count of waiting ones, and all finished ones.
