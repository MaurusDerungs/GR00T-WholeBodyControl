import sys, os, time
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, 'oculus_reader'))
import numpy as np
from oculus_reader.reader import OculusReader

LOG_FILE = os.path.join(_HERE, 'axes_log.txt')

reader = OculusReader()
home_r = home_l = None
lines = []
t0 = time.time()

def log(msg):
    print(msg)
    lines.append(msg)

log("=== axes diagnostic — both controllers ===")
log("Sequence: 1) hold still (home capture)  2) both hands DOWN  3) both hands FORWARD  4) both hands to the SIDES")
log(f"{'t(s)':>6}  {'L_X':>7} {'L_Y':>7} {'L_Z':>7}    {'R_X':>7} {'R_Y':>7} {'R_Z':>7}")
log("-" * 65)

try:
    while True:
        poses, _ = reader.get_transformations_and_buttons()
        T_r = poses.get('r')
        T_l = poses.get('l')

        if T_r is not None and home_r is None:
            home_r = T_r[:3, 3].copy()
        if T_l is not None and home_l is None:
            home_l = T_l[:3, 3].copy()

        if home_r is None or home_l is None:
            time.sleep(0.05)
            continue

        dr = T_r[:3, 3] - home_r if T_r is not None else np.zeros(3)
        dl = T_l[:3, 3] - home_l if T_l is not None else np.zeros(3)
        t = time.time() - t0

        msg = (f"{t:6.1f}  "
               f"{dl[0]:+7.3f} {dl[1]:+7.3f} {dl[2]:+7.3f}    "
               f"{dr[0]:+7.3f} {dr[1]:+7.3f} {dr[2]:+7.3f}")
        log(msg)
        time.sleep(0.2)

except KeyboardInterrupt:
    pass

with open(LOG_FILE, 'w') as f:
    f.write('\n'.join(lines))
print(f"\nSaved to {LOG_FILE}")
