"""An explicitly promoted book, pinned to the evaluated engine and book hashes."""
import json
from pathlib import Path

from .experiments import fingerprint, write_json

CONFIG = Path(__file__).resolve().parents[1] / 'books/default.json'


def default_book(engine, config=CONFIG):
    try:
        data = json.loads(config.read_text(encoding='utf-8'))
        book = Path(data['book'])
        if (data['version'] == 1 and Path(engine).resolve() == Path(data['engine'])
                and fingerprint(engine) == data['engine_sha256']
                and fingerprint(book) == data['book_sha256']):
            return str(book)
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def activate_book(engine, book, summary, config=CONFIG):
    if not summary.get('comparison', {}).get('confirmed_improvement'):
        return False
    if summary.get('technical_errors') or not summary.get('book_usage', {}).get('hits'):
        return False
    # Verify that this is still the exact candidate tested by the recorded run.
    rows = summary.get('rows', [])
    if not rows:
        return False
    manifest = json.loads((Path(rows[0]['path']).parent / 'manifest.json').read_text(encoding='utf-8'))
    tested = manifest['participants']['white']
    engine_hash, book_hash = fingerprint(engine), fingerprint(book)
    if engine_hash != tested['sha256'] or book_hash != tested['book_sha256']:
        raise ValueError('Движок или книга изменились после сравнения')
    write_json(config, dict(version=1, engine=str(Path(engine).resolve()), engine_sha256=engine_hash,
                            book=str(Path(book).resolve()), book_sha256=book_hash,
                            comparison=str((Path(rows[0]['path']).parent / 'summary.json').resolve())))
    return True
