# Dance Loop Reference Set

This directory contains the reference motions used by `launch_dance_loop.sh`.
The entries are symlinks to the existing released motion CSV folders so the
launcher can load a dance-only sequence without duplicating the large CSV files.

Current sequence:

0. `neutral_kick_R_001__A543` startup/settle reference
1. `dance_in_da_party_001__A464`
2. `dance_in_da_party_001__A464_M`
3. `macarena_001__A545`
4. `macarena_001__A545_M`

`launch_dance_loop.sh` starts playback at motion index 1, so index 0 is only
used as the quiet controlled pose before dancing begins.

Add more dances by placing or linking additional valid motion folders here.
Each motion folder must contain the same CSV files as the examples under
`gear_sonic_deploy/reference/example/`.
