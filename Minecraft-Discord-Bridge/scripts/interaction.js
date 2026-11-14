/**
 * Discord interaction responses can be acknowledged only once.  Treat an
 * acknowledgement made by another listener/process as a completed response,
 * rather than attempting a second reply and producing 40060/10062.
 */
export function isAcknowledgementError(error) {
  const code = error?.code ?? error?.rawError?.code;
  return code === 40060 || code === 10062;
}

export async function deferInteraction(interaction, { ephemeral = false } = {}) {
  if (interaction.deferred || interaction.replied) return true;

  try {
    await interaction.deferReply(ephemeral ? { flags: 64 } : undefined);
    return true;
  } catch (error) {
    // A concurrently running copy of the listener may have won the race.
    // It has already supplied the only response Discord permits.
    if (isAcknowledgementError(error)) return false;
    throw error;
  }
}
