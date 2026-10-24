"""Bounded, persistent discovery cursors, decisions and failed-detail retries."""
import copy
import logging
from datetime import datetime, timedelta

from ..db import session_scope
from ..models import Setting, now
from ..sources.douban import DoubanError, DoubanMovie
from .movies import collection_cutoff

log = logging.getLogger(__name__)
STATE_KEY = 'douban_recent_discovery'
AUDIT_LIMIT = 2000
RETRY_LIMIT = 1000
RETRY_BATCH = 20


class DiscoveryHistory:
    def __init__(self, settings):
        self.first_year = collection_cutoff(settings).year
        self.last_year = now().year
        self.criteria = dict(cutoff=collection_cutoff(settings).isoformat(),
                             min_rating=settings.collect.douban_min_rating,
                             min_votes=settings.collect.douban_min_votes)
        self.processed = set()
        self.decisions = 0
        self.added = 0
        with session_scope() as session:
            row = session.get(Setting, STATE_KEY)
            self.state = copy.deepcopy(row.value) if row and isinstance(row.value, dict) else {}
        signature = [self.first_year, self.last_year, settings.collect.max_age_years,
                     settings.collect.max_age_months, settings.collect.douban_min_rating,
                     settings.collect.douban_min_votes]
        if self.state.get('signature') != signature:
            self.state.update(signature=signature, progress={})
        self.progress = self.state.setdefault('progress', {})
        self.audit = self.state.setdefault('audit', {})
        self.retry = self.state.setdefault('retry', {})
        for identity, entry in list(self.retry.items()):
            year = entry['item'].get('year')
            if year is not None and not self.first_year <= year <= self.last_year:
                self.record(DoubanMovie(**entry['item']), 'filtered', '已超出当前收集年份范围')

    def record(self, item: DoubanMovie, outcome: str, reason: str):
        stamp = now()
        self.processed.add(item.id)
        self.decisions += 1
        self.added += outcome == 'added'
        previous = self.audit.pop(item.id, {})
        decisions = previous.get('previous', [])
        if previous:
            decisions = [*decisions, {key: previous[key] for key in ('checked_at', 'outcome', 'reason')}][-3:]
        entry = dict(id=item.id, title=item.title, year=item.year, rating=item.rating,
                     votes=item.votes, release_date=item.release_date, outcome=outcome,
                     reason=reason, checked_at=stamp.isoformat(), criteria=self.criteria,
                     first_seen_at=previous.get('first_seen_at', stamp.isoformat()), previous=decisions)
        self.audit[item.id] = entry
        while len(self.audit) > AUDIT_LIMIT:
            self.audit.pop(next(iter(self.audit)))
        if outcome == 'retry':
            if item.id not in self.retry and len(self.retry) >= RETRY_LIMIT:
                raise DoubanError('待重试条目已满，暂停翻页并保留当前页进度')
            attempts = self.retry.get(item.id, {}).get('attempts', 0) + 1
            self.retry[item.id] = dict(
                item={key: getattr(item, key) for key in ('id', 'title', 'year', 'rating', 'votes')},
                attempts=attempts,
                retry_at=(stamp + timedelta(hours=min(24, 2 ** min(attempts - 1, 5)))).isoformat())
        else:
            self.retry.pop(item.id, None)
        log.info('Recent discovery %s %s: %s (%s)', item.id, item.title, outcome, reason)

    def due_retries(self):
        entries = sorted(self.retry.values(), key=lambda entry: entry['retry_at'])
        return [DoubanMovie(**entry['item']) for entry in entries
                if datetime.fromisoformat(entry['retry_at']) <= now()][:RETRY_BATCH]

    def feed_items(self, items):
        for item in items:
            if item.id in self.processed:
                continue
            pending = self.retry.get(item.id)
            if pending and datetime.fromisoformat(pending['retry_at']) > now():
                continue
            yield item

    def save(self):
        self.state['updated_at'] = now().isoformat()
        with session_scope() as session:
            row = session.get(Setting, STATE_KEY)
            if row:
                row.value = copy.deepcopy(self.state)
            else:
                session.add(Setting(key=STATE_KEY, value=copy.deepcopy(self.state)))

    def summary(self):
        positions = [f'{year} 年第 {entry["offset"] // 50 + 1} 页'
                     for year, entry in self.progress.get('years', {}).items() if entry.get('offset')]
        continuation = '、'.join(positions[:3]) + '继续' if positions else '已到列表末尾，下轮重新扫描'
        if any(entry.get('stop_reason') == '列表重复' for entry in self.progress.get('years', {}).values()):
            continuation += '；部分年份返回重复列表，已暂停对应翻页'
        if self.state.get('last_error'):
            continuation = '来源查询失败，保留进度待重试'
        return f'近期补充核对 {self.decisions} 条，待重试 {len(self.retry)} 条；{continuation}'
