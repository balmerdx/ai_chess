#!/usr/bin/env python3
"""Export user-visible conversation from a Codex JSONL session snapshot."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--title', default='Журнал сессии — ai_chess')
    parser.add_argument('--notes', type=Path, help='Отдельная справка для продолжения')
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error('Источник и архив должны быть разными файлами')
    snapshot = args.source.read_bytes()
    # A live session may end with an incomplete JSONL record. Ignore only that tail.
    lines = snapshot.splitlines(keepends=True)
    entries = []
    calls = set()
    metadata = {}
    messages = 0
    question_blocks = 0
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            if index == len(lines) - 1 and not line.endswith(b'\n'):
                break
            raise
        payload = record.get('payload', {})
        if record.get('type') == 'session_meta':
            metadata = payload
        if record.get('type') != 'response_item':
            continue
        kind = payload.get('type')
        label = None
        body = None
        if kind == 'message' and payload.get('role') in ('user', 'assistant'):
            if payload.get('role') == 'assistant' and payload.get('channel') not in (None, 'commentary', 'final'):
                continue
            parts = payload.get('content', [])
            body = '\n'.join(part['text'] for part in parts if part.get('type') in ('input_text', 'output_text', 'text'))
            attachments = [part.get('type', 'unknown') for part in parts if part.get('type') not in ('input_text', 'output_text', 'text')]
            if attachments:
                body += '\n\n[Нетекстовые вложения: ' + ', '.join(attachments) + '; см. исходную историю.]'
            label = 'Пользователь' if payload['role'] == 'user' else 'Ассистент'
            messages += 1
        elif kind == 'function_call' and 'request_user_input' in payload.get('name', ''):
            calls.add(payload['call_id'])
            label = 'Вопросы выбора параметров'
            body = '```json\n' + json.dumps(json.loads(payload['arguments']), ensure_ascii=False, indent=2) + '\n```'
            question_blocks += 1
        elif kind == 'function_call_output' and payload.get('call_id') in calls:
            label = 'Ответы пользователя на вопросы выбора параметров'
            body = '```json\n' + json.dumps(json.loads(payload['output']), ensure_ascii=False, indent=2) + '\n```'
        if label is not None:
            entries.append(f"### {len(entries) + 1}. {label} — {record.get('timestamp', 'время неизвестно')}\n\n{body}\n")
    if not messages:
        parser.error('В источнике не найдены сообщения переписки')
    now = datetime.now(timezone.utc).isoformat()
    header = (
        f'# {args.title}\n\n'
        f"Проект: `{metadata.get('cwd', 'не указан')}`. Время сообщений — UTC.\n\n"
        'Текст сообщений пользователя и публичных ответов ассистента перенесён без пересказа. '
        'Системные и служебные инструкции, внутренние рассуждения, сводки сжатия контекста '
        'и машинные трассы инструментов исключены. Вопросы выбора параметров и ответы сохранены отдельно.\n\n'
        f'Снимок: {now}; сообщений: {messages}; блоков вопросов: {question_blocks}. '
        'Ответ о завершении архивирования может следовать после этого снимка.\n\n'
        f'Источник: `{args.source.name}`; ID сессии: `{metadata.get("id", "не указан")}`. '
        f'SHA-256 прочитанного снимка JSONL: `{hashlib.sha256(snapshot).hexdigest()}`. '
        'Исходная история может продолжать пополняться после снимка.\n\n'
    )
    if args.notes:
        header += '## Справка для следующей сессии (сводка, не дословная переписка)\n\n' + args.notes.read_text().strip() + '\n\n'
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + '.tmp')
    temporary.write_text((header + '## Переписка\n\n' + '\n'.join(entries)).rstrip('\n') + '\n')
    temporary.replace(args.output)
    print(f'{args.output}: {messages} сообщений, {question_blocks} блоков вопросов')


if __name__ == '__main__':
    main()
