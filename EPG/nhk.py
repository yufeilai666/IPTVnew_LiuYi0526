# -*- coding:utf-8 -*-
import datetime
import os
import asyncio
import httpx


NHK_API_BASE = 'https://api.nhk.jp/r8/pg/date'
DEFAULT_NHK_AREA_ID = '130'
DEFAULT_NHK_MEDIA = 'tv'
_NHK_RESPONSE_CACHE = {}
_NHK_INFLIGHT_REQUESTS = {}


def _parse_nhk_datetime(value):
    if value.endswith('Z'):
        value = value[:-1] + '+00:00'
    return datetime.datetime.fromisoformat(value)


def _build_desc(program):
    desc_parts = []
    for key in ('description', 'longDescription'):
        text = program.get(key)
        if text:
            desc_parts.append(text)
    return '\n'.join(desc_parts)


async def _request_nhk_data(url):
    async with httpx.AsyncClient() as client:
        res = await client.get(
            url,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=10,
        )
    res.raise_for_status()
    return res.json()


async def _fetch_nhk_data(url):
    cached_data = _NHK_RESPONSE_CACHE.get(url)
    if cached_data is not None:
        return cached_data

    task = _NHK_INFLIGHT_REQUESTS.get(url)
    if task is None:
        task = asyncio.create_task(_request_nhk_data(url))
        _NHK_INFLIGHT_REQUESTS[url] = task

    try:
        data = await task
    except Exception:
        if _NHK_INFLIGHT_REQUESTS.get(url) is task:
            _NHK_INFLIGHT_REQUESTS.pop(url, None)
        raise

    _NHK_RESPONSE_CACHE[url] = data
    if _NHK_INFLIGHT_REQUESTS.get(url) is task:
        _NHK_INFLIGHT_REQUESTS.pop(url, None)
    return data


async def get_epgs_nhk(channel, dt):
    epgs = []
    msg = ''
    success = 1
    channel_id = channel['id']
    service_id = channel['id0']
    area_id = channel.get('area_id', DEFAULT_NHK_AREA_ID)
    media = channel.get('media', DEFAULT_NHK_MEDIA)
    date_str = dt.strftime('%Y-%m-%d')
    url = f'{NHK_API_BASE}/{media}/{area_id}/{date_str}.json'
    try:
        data = await _fetch_nhk_data(url)
        service = data.get(service_id)
        if not isinstance(service, dict):
            raise ValueError(f'NHK service not found: {service_id}')
        programs = service.get('publication')
        if not isinstance(programs, list):
            raise ValueError(f'NHK publication not found: {service_id}')
        for program in programs:
            start_date = program.get('startDate')
            end_date = program.get('endDate')
            title = program.get('name', '')
            misc = program.get("misc") or {}
            if misc.get("isChangeable"):
                title += "[変更あり]"
            display_audio_modes = misc.get("displayAudioMode") or []
            if "ch222" in display_audio_modes:
                title += "[22.2]"
            if "ch51" in display_audio_modes:
                title += "[5.1]"
            if "stereo" in display_audio_modes:
                title += "[S]"
            if "lang2" in display_audio_modes:
                title += "[二]"
            if "lang3" in display_audio_modes:
                title += "[三]"
            if "lang4" in display_audio_modes:
                title += "[四]"
            if "multiple" in display_audio_modes:
                title += "[多]"
            if "kaisetsu" in display_audio_modes:
                title += "[解]"
            if misc.get("supportCaption"):
                title += "[字]"
            if misc.get("supportDataBroadcast") and not misc.get("isInteractive"):
                title += "[デ]"
            if misc.get("supportSign"):
                title += "[手]"
            if misc.get("isInteractive"):
                title += "[双]"
            if misc.get("supportHybridcast"):
                title += "[HC]"
            displayVideoRange = misc.get("displayVideoRange")
            if displayVideoRange == "hdr":
                title += "[HDR]"
            releaseLevel = misc.get("releaseLevel")
            if releaseLevel == "repeat":
                title += "[再]"
            # print(title)
            if not start_date or not end_date or not title:
                continue
            epg = {
                'channel_id': channel_id,
                'starttime': _parse_nhk_datetime(start_date),
                'endtime': _parse_nhk_datetime(end_date),
                'title': title,
                'desc': _build_desc(program),
            }
            # print(epg)
            epgs.append(epg)
    except Exception as e:
        success = 0
        spidername = os.path.basename(__file__).split('.')[0]
        msg = 'spider-%s-%s' % (spidername, e)
    ret = {
        'success': success,
        'epgs': epgs,
        'msg': msg,
        'ban': 0,
    }
    return ret


# asyncio.run(get_epgs_nhk({'id': 'nhk_bsp4k', 'name': 'NHK BSP4K', 'id0': 's5', 'source': 'nhk'}, datetime.datetime.now().date()))
