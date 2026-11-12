-- 0009: two more kinds of worker job on the collection queue.
--
-- `retraction_sweep` re-checks cited papers against the retraction record every
-- week. It was a launchd job on one laptop, so a deployed stack never ran it.
-- `mesh_scan` is the trend scan, moved out of the API process so it shares the
-- worker's PubMed rate limit instead of doubling it.

BEGIN;

ALTER TABLE collection_runs DROP CONSTRAINT collection_runs_kind_check;
ALTER TABLE collection_runs ADD CONSTRAINT collection_runs_kind_check CHECK (
    kind IN ('social_daily', 'social_weekly', 'product_discovery',
             'retraction_sweep', 'mesh_scan')
);

COMMIT;
