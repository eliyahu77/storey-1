# Copyright 2020 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from .utils import parse_duration

bucketPerWindow = 10


class WindowBase:
    def __init__(self, window, period, window_str):
        self.window_millis = window
        self.period_millis = period
        self.window_str = window_str


class FixedWindow(WindowBase):
    def __init__(self, window):
        window_millis = parse_duration(window)
        WindowBase.__init__(
            self, window_millis, window_millis / bucketPerWindow, window)

    def get_total_number_of_buckets(self):
        return bucketPerWindow * 2


class SlidingWindow(WindowBase):
    def __init__(self, window, period):
        window_millis, period_millis = \
            parse_duration(window), parse_duration(period)
        if not window_millis % period_millis == 0:
            raise Exception('period must be a divider of the window')

        WindowBase.__init__(self, window_millis, period_millis, window)

    def get_total_number_of_buckets(self):
        return int(self.window_millis / self.period_millis)


class EmitAfterPeriod:
    pass


class EmitAfterWindow:
    pass


class EmitAfterMaxEvent:
    def __init__(self, max_events):
        self.max_events = max_events


class EmitEveryEvent:
    pass
