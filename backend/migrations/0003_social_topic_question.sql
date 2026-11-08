-- 0003: the question a social topic's article would answer.
--
-- Written once by a model call over the topic's post titles (app/social/questions.py).
-- NULL means not written yet: the desk and a promotion then use the label.

BEGIN;

ALTER TABLE social_topics ADD COLUMN question TEXT;

COMMIT;
