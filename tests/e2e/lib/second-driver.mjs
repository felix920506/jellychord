// A second requester for scenarios that need two users (fairplay): a second
// bot account that sends control requests itself, so the bot under test sees
// a real, separate member with its own voice state. The bot accepts it via
// JELLYCHORD_E2E_EXTRA_DRIVER_IDS. See jellychord/e2e_control.py.

// Hook command listing the user IDs the bot accepts control requests from.
export const DRIVERS_COMMAND = 'drivers';

// What Driver.command throws when the bot's E2E hook predates extra drivers:
// it doesn't know the `drivers` command (and ignores the second account).
const UNSUPPORTED = new RegExp(`^bot raised while handling "${DRIVERS_COMMAND}": UnknownCommand: ${DRIVERS_COMMAND}$`);

// Decides what a failed `drivers` probe means. The published image (docker
// mode) can be older than the checkout, so exactly "unknown command" there
// skips the scenarios that need a second user. Any other error, and any error
// when running the checkout itself, fails the run.
export function secondDriverProbeOutcome(err, sutMode) {
  if (!err) return 'supported';
  if (sutMode === 'docker' && UNSUPPORTED.test(err.message ?? '')) return 'skip';
  return 'fail';
}
