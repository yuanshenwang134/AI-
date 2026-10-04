"""Parse score text without treating clocks or period numbers as scores."""
import re

_NUMBER = r'(?<![\w:])\d{1,3}(?![\w:])'

def clean_text(text):
    text = text or ''
    if re.search(r'[A-Za-z]', text):
        text = re.sub(r'\b\d{1,2}:\d{2}(?:\.\d+)?\b', ' ', text)
    return re.sub(r'\b\d+\s*(?:st|nd|rd|th)?\s*(?:qtr|quarter|period|half)\b',
                  ' ', text, flags=re.I)

def parse_number(text):
    values = re.findall(_NUMBER, clean_text(text))
    return int(values[0]) if len(values) == 1 else None

def parse_scores(text, team_names=None):
    text = clean_text(text)
    if team_names:
        labels = [team_names[t] for t in ('home', 'away')]
        pattern = '|'.join(re.escape(x) for x in labels)
        found = list(re.finditer(r'\b(?:' + pattern + r')\b', text, re.I))
        values = {}
        for i, m in enumerate(found):
            stop = found[i+1].start() if i+1 < len(found) else len(text)
            value = re.match(r'\s*[:=]?\s*(\d{1,3})\b', text[m.end():stop])
            if value:
                side = next(t for t in ('home', 'away') if team_names[t].lower() == m[0].lower())
                values[side] = int(value[1])
        return (values if len(values) == 2 else None), 'team_labels'
    # Require a label immediately before each score. A missing score never
    # borrows the next team's score or the quarter number.
    pairs = re.findall(r'\b([A-Za-z][A-Za-z0-9._-]{1,15})\s+(\d{1,3})\b', text)
    pairs = [(name, int(value)) for name, value in pairs
             if name.lower() not in {'qtr', 'quarter', 'period', 'half', 'shot', 'clock'}]
    if len(pairs) == 2 and pairs[0][0].lower() != pairs[1][0].lower():
        return {'home': pairs[0][1], 'away': pairs[1][1]}, 'team_labels'
    # A partial labelled overlay must not fall back to unlabelled numbers.
    if re.search(r'[A-Za-z]', text):
        return None, 'incomplete_labels'
    numbers = re.findall(_NUMBER, text.replace(':', ' '))
    if len(numbers) == 2:
        return dict(zip(('home', 'away'), map(int, numbers))), 'position_fallback'
    return None, 'unreadable'

def build_events(items, texts, start=None, max_delta=3, team_names=None):
    start = start or {}
    current = {t: (int(start[t]) if start.get(t) is not None else None) for t in ('home','away')}
    baseline = dict(current)
    origin = {t: 'provided' if current[t] is not None else 'video' for t in current}
    events, rejected, warnings, readings = [], [], [], []
    warned = set()
    for item in sorted(items, key=lambda x:x['t']):
        text = texts.get(item['file'], '')
        if item['team'] == 'all':
            values, mode = parse_scores(text, team_names)
        else:
            value = parse_number(text)
            values = {item['team']:value} if value is not None else None
            mode = 'team_crop'
        if not values:
            continue
        readings.append({'t':item['t'], 'values':values, 'parse_mode':mode,
                         'confidence':0.5 if mode == 'position_fallback' else 1.0})
        invalid = []
        for team, value in values.items():
            prev = current[team]
            if prev is not None and not 0 <= value-prev <= max_delta:
                invalid.append({'t':item['t'],'team':team,'read':value,'prev':prev,
                                'why':'读数回退或跳变超过允许增量'})
                if team not in warned and team in start and abs(value-int(start[team])) > 20:
                    warnings.append(f'{team} 读数与提供的起始比分相差超过 20 分，请检查起始比分')
                    warned.add(team)
        if invalid:
            rejected.extend(invalid)
            continue  # Whole overlay rejected: do not accept its other team's digit.
        for team, value in values.items():
            if current[team] is None:
                current[team] = baseline[team] = value
            elif value > current[team]:
                events.append({'t':item['t'],'team':team,'delta':value-current[team],
                               'value':value,'parse_mode':mode,
                               'confidence':0.5 if mode == 'position_fallback' else 0.9})
                current[team] = value
    return {'events':events,'rejected':rejected,'final':current,'start':baseline,
            'baseline_from': 'provided' if all(v=='provided' for v in origin.values()) else
                             ('video' if all(v=='video' for v in origin.values()) else 'mixed'),
            'baseline_sources':origin,'warnings':warnings,'readings':readings,
            'ocr_hit':len(readings)}
