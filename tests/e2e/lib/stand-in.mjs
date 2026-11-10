// A second requester for scenarios that need two users (fairplay). The test
// has one Discord account, so a control request can carry `e2e.as=<user-id>`
// to run the command as another, made-up user ID that shares the listener's
// voice state. See jellychord/e2e_control.py.

export const STAND_IN_ARG = 'e2e.as';
export const STAND_IN_USER_ID = '1';

// What Driver.command throws when the bot's E2E hook predates e2e.as: its
// argument binding rejects the key as an unknown option.
const UNSUPPORTED = /^bot raised while handling "[^"]+": ParseError: unknown option\(s\): e2e\.as$/;

// Decides what a failed stand-in probe means. The published image (docker
// mode) can be older than the checkout, so exactly "e2e.as unknown" there
// skips the scenarios that need it. Any other error, and any error when
// running the checkout itself, fails the run.
export function standInProbeOutcome(err, sutMode) {
  if (!err) return 'supported';
  if (sutMode === 'docker' && UNSUPPORTED.test(err.message ?? '')) return 'skip';
  return 'fail';
}
