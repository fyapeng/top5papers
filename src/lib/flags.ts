export type Window = { weekAgo: string; recent: string; tracked: string };

// "new": went online this week, or picked up this week by a run after the first one (the first run and any
// backfill stamp first_seen on everything, so first_seen alone would flag old articles)
export const isNew = (p: any, w: Window) =>
  p.online >= w.weekAgo || (p.first_seen > w.tracked && p.first_seen >= w.weekAgo && p.online >= w.recent);

export const newlyIssued = (p: any, w: Window) => !isNew(p, w) && (p.issued_seen || "") >= w.weekAgo;

export const anchor = (p: any) => `p-${String(p.doi).toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
