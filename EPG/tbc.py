# -*- coding:utf-8 -*-
import asyncio
from datetime import datetime, timedelta
import os
import re
from zoneinfo import ZoneInfo

import httpx
from bs4 import BeautifulSoup as bs


# TBC_CHANNEL_URL = 'https://www.tbc.net.tw/EPG/Epg/ChannelV2'
TBC_CHANNEL_URL = 'https://api.liuyi0526.com/tbc/EPG/Epg/ChannelV2'
TBC_CHANNELS_URL = 'https://www.tbc.net.tw/EPG/Epg/IndexV2'
TBC_TIMEZONE = ZoneInfo('Asia/Taipei')
TBC_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
TBC_HEADERS = {
    'User-Agent': 'Mozilla/5.0',
}


async def get_epgs_tbc(channel):
    epgs = []
    msg = ''
    success = 1
    channel_id = channel['id']
    channel_id0 = str(channel['id0'])
    try:
        async with httpx.AsyncClient(
            timeout=TBC_TIMEOUT,
            follow_redirects=True,
        ) as client:
            res = await client.get(
                TBC_CHANNEL_URL,
                params={'channelId': channel_id0},
                headers=TBC_HEADERS,
            )
        res.raise_for_status()
        res.encoding = 'utf-8'
        soup = bs(res.text, 'html.parser')
        programme_nodes = soup.select('ul.list_program2 li')
        if not programme_nodes:
            raise ValueError(f'TBC channel has no programmes: {channel_id0}')

        seen = set()
        for node in programme_nodes:
            date_text = node.get('date', '').strip()
            time_text = node.get('time', '').strip()
            time_match = re.fullmatch(
                r'(\d{1,2}:\d{2})\s*~\s*(\d{1,2}:\d{2})',
                time_text,
            )
            if not date_text or time_match is None:
                continue

            start_text, end_text = time_match.groups()
            starttime = datetime.strptime(
                f'{date_text} {start_text}',
                '%Y/%m/%d %H:%M',
            ).replace(tzinfo=TBC_TIMEZONE)
            endtime = datetime.strptime(
                f'{date_text} {end_text}',
                '%Y/%m/%d %H:%M',
            ).replace(tzinfo=TBC_TIMEZONE)
            if endtime <= starttime:
                endtime += timedelta(days=1)

            title = (
                node.get('title')
                or node.get('alt')
                or node.get_text(' ', strip=True)
            )
            if not title:
                continue
            desc = node.get('desc', '')
            programme_key = (starttime, endtime, title)
            if programme_key in seen:
                continue
            seen.add(programme_key)
            epgs.append({
                'channel_id': channel_id,
                'starttime': starttime,
                'endtime': endtime,
                'title': title,
                'desc': desc,
            })
        epgs.sort(key=lambda item: item['starttime'])
        for i in epgs:
            print(i)
    except Exception as e:
        success = 0
        spidername = os.path.splitext(os.path.basename(__file__))[0]
        msg = 'spider-%s-%s-%s' % (spidername, type(e).__name__, e)
    ret = {
        'success': success,
        'epgs': epgs,
        'msg': msg,
        'ban': 0,
    }
    return ret


# 下载TBC所有频道ID及名称
async def get_channels_tbc():
    channels = []
    async with httpx.AsyncClient(
        timeout=TBC_TIMEOUT,
        follow_redirects=True,
    ) as client:
        res = await client.get(TBC_CHANNELS_URL, headers=TBC_HEADERS)
    res.raise_for_status()
    res.encoding = 'utf-8'
    soup = bs(res.text, 'html.parser')
    lis = soup.select('ul.list_tv > li')
    for li in lis:
        name = li['title']
        channel_id = li.find("span", class_="num").text
        channel_id0 = li['id']
        # img = li.select('img')[0]['src']
        # url = li.a['href']
        channel = {
            'id': 'tbc_' + channel_id.zfill(3),
            'name': name,
            'id0': channel_id0,
            'source': 'tbc',
        }
        print(channel)
        channels.append(channel)
    return channels


if __name__ == '__main__':
    asyncio.run(get_channels_tbc())
    # asyncio.run(get_epgs_tbc({'id': 'tbc_002', 'name': '南桃園節目總表', 'id0': '662', 'source': 'tbc'}))
