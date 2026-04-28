#!/usr/bin/env python3
"""Import barriers_rfea.lss into a LimeSurvey instance via RemoteControl 2.

Setup (one-time):
    1. In LimeSurvey admin: Configuration -> Settings -> Interfaces ->
       RPC interface type = JSON-RPC -> Save.
    2. Create a `.env` file alongside this script with:
         LIME_URL=https://tsesurvey.limequery.com
         LIME_USER=<your-admin-username>
         LIME_PASS=<your-password>

Usage:
    python lime_import.py                    # first-time import
    python lime_import.py --replace 123456   # replace existing SID
    python lime_import.py --check            # auth-only sanity check

Requirements: Python >= 3.7, stdlib only.
"""
import argparse
import base64
import sys
from pathlib import Path

from lime_rpc import add_auth_args, authenticate, load_dotenv, rpc

load_dotenv(Path(__file__).parent)


def main():
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split('\n', 2)[2],
    )
    p.add_argument('--lss', default='barriers_rfea.lss',
                   help='Path to .lss file (default: %(default)s)')
    add_auth_args(p)
    p.add_argument('--replace', type=int, metavar='SID',
                   help='Delete this SID before importing (otherwise creates a new survey)')
    p.add_argument('--check', action='store_true',
                   help='Authenticate and exit; do not import')
    args = p.parse_args()

    print(f'Authenticating as {args.user!r}')
    endpoint, session = authenticate(args)
    print('  authenticated')

    try:
        if args.check:
            print('Auth OK. RC2 endpoint is reachable.')
            return 0

        lss_path = Path(args.lss)
        if not lss_path.exists():
            sys.exit(f'LSS file not found: {lss_path}')

        if args.replace is not None:
            print(f'Deleting existing SID {args.replace}')
            del_result = rpc(endpoint, 'delete_survey', [session, args.replace])
            print(f'  delete result: {del_result}')

        encoded = base64.b64encode(lss_path.read_bytes()).decode('ascii')
        print(f'Importing {lss_path} ({lss_path.stat().st_size:,} bytes)')
        try:
            new_sid = rpc(endpoint, 'import_survey', [session, encoded, 'lss'])
        except SystemExit:
            if args.replace is not None:
                print('WARNING: delete succeeded but import failed. The previous survey is gone.')
                print('Re-run without --replace to create a fresh survey.')
            raise
        if not isinstance(new_sid, int):
            sys.exit(f'Import did not return a SID. Result: {new_sid!r}')
        print(f'  imported as SID {new_sid}')

        props = rpc(endpoint, 'get_language_properties',
                    [session, new_sid, ['surveyls_welcometext'], 'en'])
        welcome = props.get('surveyls_welcometext', '') if isinstance(props, dict) else ''
        if '{SID}' in welcome:
            patched = welcome.replace('{SID}', str(new_sid))
            rpc(endpoint, 'set_language_properties',
                [session, new_sid, {'surveyls_welcometext': patched}, 'en'])
            print(f'  welcome text: {{SID}} -> {new_sid} substituted')
        else:
            print('  welcome text: no {SID} placeholder found (skipped)')

        # Substitute {SID} in group descriptions (bws_reset.js script tag)
        groups = rpc(endpoint, 'list_groups', [session, new_sid])
        patched_count = 0
        if isinstance(groups, list):
            for g in groups:
                gid = g['gid']
                group_props = rpc(endpoint, 'get_group_properties',
                                  [session, gid, ['description'], 'en'])
                desc = group_props.get('description', '') if isinstance(group_props, dict) else ''
                if '{SID}' in desc:
                    rpc(endpoint, 'set_group_properties',
                        [session, gid,
                         {'questiongroupl10ns': {'en': {'description': desc.replace('{SID}', str(new_sid))}}}])
                    patched_count += 1
        print(f'  group descriptions: substituted {{SID}} in {patched_count} groups')

        base = args.url.rstrip('/')
        print()
        print(f'Admin:   {base}/index.php/surveyAdministration/view?surveyid={new_sid}')
        print(f'Preview: {base}/index.php/{new_sid}')
        print()
        print(f'Manual steps for SID {new_sid}:')
        print(f'  1. Upload Barriers_RF.png  -> upload/surveys/{new_sid}/images/')
        print(f'  2. Upload bws_reset.js     -> upload/surveys/{new_sid}/files/')
        print(f'(LimeSurvey admin: Resources -> Files -> Upload)')
        print(f'Verify "Filter HTML for XSS" is Off in Global Settings -> Security.')

        return 0

    finally:
        rpc(endpoint, 'release_session_key', [session])


if __name__ == '__main__':
    sys.exit(main())
