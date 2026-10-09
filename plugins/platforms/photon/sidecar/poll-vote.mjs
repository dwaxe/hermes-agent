// Missing or malformed selection state must never become consent.
export function normalizePollVote(content) {
  return {
    type: "poll_option",
    title: content.option?.title ?? content.title ?? "",
    selected: content.selected === true,
    pollTitle: content.poll?.title ?? null,
  };
}
