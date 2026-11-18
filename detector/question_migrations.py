"""One-time default changes preserve custom questions and completed snapshots."""
import json

from .console_store import dumps
from .quality_catalog import defaults, validate


def candy_single(db):
    marker = 'candy_single_v4'
    if db.execute('SELECT 1 FROM console_documents WHERE key=?', (marker,)).fetchone():
        return
    candidate = defaults()[0]
    legacy = {**candidate, 'title': '糖果双次校验', 'rounds': 2}

    def upgrade(questions):
        changed = False
        for index, question in enumerate(questions):
            if {k: v for k, v in question.items() if k != 'version'} == legacy:
                questions[index] = validate([candidate])[0]
                changed = True
        return changed

    row = db.execute("SELECT value FROM console_documents WHERE key='questions'").fetchone()
    questions = json.loads(row['value'])
    if upgrade(questions):
        db.execute("UPDATE console_documents SET value=?,revision=revision+1 WHERE key='questions'", (dumps(questions),))
    for row in db.execute('SELECT id,value FROM console_plans').fetchall():
        value = json.loads(row['value'])
        if upgrade(value.get('questions', [])):
            db.execute('UPDATE console_plans SET value=?,revision=revision+1 WHERE id=?', (dumps(value), row['id']))
    db.execute('INSERT INTO console_documents VALUES (?,1,?)', (marker, '{}'))
