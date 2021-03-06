# SPDX-License-Identifier: Apache-2.0
#
# Copyright (C) 2019, Arm Limited and contributors.
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import os
import re
import glob
from collections import namedtuple

import pandas as pd

from lisa.analysis.base import AnalysisHelpers, TraceAnalysisBase
from lisa.datautils import df_filter_task_ids, df_window, df_split_signals
from lisa.trace import TaskID, requires_events, requires_one_event_of, may_use_events, MissingTraceEventError
from lisa.utils import deprecate, memoized
from lisa.analysis.tasks import TasksAnalysis


RefTime = namedtuple("RefTime", ['kernel', 'user'])
"""
Named tuple to synchronize kernel and userspace (``rt-app``) timestamps.
"""


PhaseWindow = namedtuple("PhaseWindow", ['id', 'start', 'end'])
"""
Named tuple with fields:

    * ``id``: integer ID of the phase
    * ``start``: timestamp of the start of the phase
    * ``end``: timestamp of the end of the phase

"""


class RTAEventsAnalysis(TraceAnalysisBase):
    """
    Support for RTA events analysis.

    :param trace: input Trace object
    :type trace: lisa.trace.Trace
    """

    name = 'rta'

    RTAPP_USERSPACE_EVENTS = [
        'rtapp_main',
        'rtapp_task',
        'rtapp_loop',
        'rtapp_event',
        'rtapp_stats',
    ]
    """
    List of ftrace events rtapp is able to emit.
    """

    def _task_filtered(self, df, task=None):
        if not task:
            return df

        task = self.trace.get_task_id(task)

        if task not in self.rtapp_tasks:
            raise ValueError("Task [{}] is not an rt-app task: {}"
                             .format(task, self.rtapp_tasks))

        return df_filter_task_ids(df, [task],
                                  pid_col='__pid', comm_col='__comm')

    @property
    @memoized
    @requires_one_event_of(*RTAPP_USERSPACE_EVENTS)
    def rtapp_tasks(self):
        """
        List of :class:`lisa.trace.TaskID` of the ``rt-app`` tasks present in
        the trace.
        """
        task_ids = set()
        for event in self.RTAPP_USERSPACE_EVENTS:
            try:
                df = self.trace.df_events(event)
            except MissingTraceEventError:
                continue
            else:
                for pid, name in df[['__pid', '__comm']].drop_duplicates().values:
                    task_ids.add(TaskID(pid, name))

        return sorted(task_ids)

###############################################################################
# DataFrame Getter Methods
###############################################################################

    ###########################################################################
    # rtapp_main events related methods
    ###########################################################################

    @requires_events('rtapp_main')
    def df_rtapp_main(self):
        """
        Dataframe of events generated by the rt-app main task.

        :returns: a :class:`pandas.DataFrame` with:

          * A ``__comm`` column: the actual rt-app trace task name
          * A ``__cpu``  column: the CPU on which the task was running at event
                                 generation time
          * A ``__line`` column: the ftrace line numer
          * A ``__pid``  column: the PID of the task
          * A ``data``   column: the data corresponding to the reported event
          * An ``event`` column: the event generated

        The ``event`` column can report these events:

          * ``start``: the start of the rt-app main thread execution
          * ``end``: the end of the rt-app main thread execution
          * ``clock_ref``: the time rt-app gets the clock to be used for logfile entries

        The ``data`` column reports:

          * the base timestamp used for logfile generated event for the ``clock_ref`` event
          * ``NaN`` for all the other events

        """
        return self.trace.df_events('rtapp_main')

    @property
    @memoized
    @df_rtapp_main.used_events
    def rtapp_window(self):
        """
        Return the time range the rt-app main thread executed.

        :returns: a tuple(start_time, end_time)
        """
        df = self.df_rtapp_main()
        return (
            df[df.event == 'start'].index[0],
            df[df.event == 'end'].index[0])

    @property
    @memoized
    @df_rtapp_main.used_events
    def rtapp_reftime(self):
        """
        Return the tuple representing the ``kernel`` and ``user`` timestamp.

        RTApp log events timestamps are generated by the kernel ftrace
        infrastructure. This method allows to know which trace timestamp
        corresponds to the rt-app generated timestamps stored in log files.

        :returns: a :class:`RefTime` reporting ``kernel`` and ``user``
                  timestamps.
        """
        df = self.df_rtapp_main()
        df = df[df['event'] == 'clock_ref']
        return RefTime(df.index[0], df.data.iloc[0])

    ###########################################################################
    # rtapp_task events related methods
    ###########################################################################

    @TraceAnalysisBase.cache
    @requires_events('rtapp_task')
    def df_rtapp_task(self, task=None):
        """
        Dataframe of events generated by each rt-app generated task.

        :param task: the (optional) rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :returns: a :class:`pandas.DataFrame` with:

          * A ``__comm`` column: the actual rt-app trace task name
          * A ``__cpu``  column: the CPU on which the task was running at event
                                 generation time
          * A ``__line`` column: the ftrace line numer
          * A ``__pid``  column: the PID of the task
          * An ``event`` column: the event generated

        The ``event`` column can report these events:

          * ``start``: the start of the ``__pid``:``__comm`` task execution
          * ``end``: the end of the ``__pid``:``__comm`` task execution

        """
        df = self.trace.df_events('rtapp_task')
        return self._task_filtered(df, task)

    ###########################################################################
    # rtapp_loop events related methods
    ###########################################################################

    @TraceAnalysisBase.cache
    @requires_events('rtapp_loop')
    def df_rtapp_loop(self, task=None):
        """
        Dataframe of events generated by each rt-app generated task.

        :param task: the (optional) rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :returns: a :class:`pandas.DataFrame` with:

          * A  ``__comm`` column: the actual rt-app trace task name
          * A  ``__cpu``  column: the CPU on which the task was running at event
                                 generation time
          * A  ``__line`` column: the ftrace line numer
          * A  ``__pid``  column: the PID of the task
          * An ``event``  column: the generated event
          * A  ``phase``  column: the phases counter for each ``__pid``:``__comm`` task
          * A  ``phase_loop``  colum: the phase_loops's counter
          * A  ``thread_loop`` column: the thread_loop's counter

        The ``event`` column can report these events:

          * ``start``: the start of the ``__pid``:``__comm`` related event
          * ``end``: the end of the ``__pid``:``__comm`` related event

        """
        df = self.trace.df_events('rtapp_loop')
        return self._task_filtered(df, task)

    @TraceAnalysisBase.cache
    @df_rtapp_loop.used_events
    def _get_rtapp_phases(self, event, task):
        df = self.df_rtapp_loop(task)
        df = df[df.event == event]

        # Sort START/END phase loop event from newers/older and...
        if event == 'start':
            df = df[df.phase_loop == 0]
        elif event == 'end':
            df = df.sort_values(by=['phase_loop', 'thread_loop'],
                                ascending=False)
        # ... keep only the newest/oldest event
        df = df.groupby(['__comm', '__pid', 'phase', 'event']).head(1)

        # Reorder the index and keep only required cols
        df = (df.sort_index()[['__comm', '__pid', 'phase']]
              .reset_index()
              .set_index(['__comm', '__pid', 'phase']))

        return df

    @TraceAnalysisBase.cache
    @df_rtapp_loop.used_events
    def df_phases(self, task):
        """
        Get phases actual start times and durations

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :returns: A :class:`pandas.DataFrame` with index representing the
            start time of a phase and these column:

                * ``phase``: the phase number.
                * ``duration``: the measured phase duration.
        """
        # Trace windowing can cut the trace anywhere, so we need to remove the
        # partial loops records to avoid confusion
        def filter_partial_loop(df):
            # Get rid of the end of a previous loop
            if not df.empty and df['event'].iloc[0] == 'end':
                df = df.iloc[1:]

            # Get rid of the beginning of a new loop at the end
            if not df.empty and df['event'].iloc[-1] == 'start':
                df = df.iloc[:-1]

            return df

        def get_duration(phase, df):
            start = df.index[0]
            end = df.index[-1]
            duration = end - start
            return (start, {'phase': phase, 'duration': duration})

        loops_df = self.df_rtapp_loop(task)

        phases_df_list = [
            (cols['phase'], filter_partial_loop(df))
            for cols, df in df_split_signals(loops_df, ['phase'])
        ]
        durations = sorted(
            get_duration(phase, df)
            for phase, df in phases_df_list
            if not df.empty
        )

        if durations:
            index, columns = zip(*durations)
            return pd.DataFrame(columns, index=index)
        else:
            return pd.DataFrame()


    @df_phases.used_events
    def task_phase_windows(self, task):
        """
        Yield the phases of the specified task.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        Yield :class: `namedtuple` reporting:

            * `id` : the iteration ID
            * `start` : the iteration start time
            * `end` : the iteration end time

        :return: Generator yielding :class:`PhaseWindow` with
            start end end timestamps.
        """
        for idx, phase in enumerate(self.df_phases(task).itertuples()):
            yield PhaseWindow(idx, phase.Index,
                              phase.Index + phase.duration)

    @_get_rtapp_phases.used_events
    def df_rtapp_phases_start(self, task=None):
        """
        Dataframe of phases start times.

        :param task: the (optional) rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :returns: a :class:`pandas.DataFrame` with:

          * A  ``__comm`` column: the actual rt-app trace task name
          * A  ``__pid``  column: the PID of the task
          * A  ``phase``  column: the phases counter for each ``__pid``:``__comm`` task

        The ``index`` represents the timestamp of a phase start event.
        """
        return self._get_rtapp_phases('start', task)

    @_get_rtapp_phases.used_events
    def df_rtapp_phases_end(self, task=None):
        """
        Dataframe of phases end times.

        :param task: the (optional) rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :returns: a :class:`pandas.DataFrame` with:

          * A  ``__comm`` column: the actual rt-app trace task name
          * A  ``__pid``  column: the PID of the task
          * A  ``phase``  column: the phases counter for each ``__pid``:``__comm`` task

        The ``index`` represents the timestamp of a phase end event.
        """
        return self._get_rtapp_phases('end', task)

    @df_rtapp_phases_start.used_events
    def _get_task_phase(self, event, task, phase):
        task = self.trace.get_task_id(task)
        if event == 'start':
            df = self.df_rtapp_phases_start(task)
        elif event == 'end':
            df = self.df_rtapp_phases_end(task)
        if phase and phase < 0:
            phase += len(df)
        phase += 1  # because of the followig "head().tail()" filter
        return df.loc[task.comm].head(phase).tail(1).Time.iloc[0]

    @_get_task_phase.used_events
    def df_rtapp_phase_start(self, task, phase=0):
        """
        Start of the specified phase for a given task.

        A negative phase value can be used to count from the oldest, e.g. -1
        represents the last phase.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :param phase: the ID of the phase start to return (default 0)
        :type phase: int

        :returns: the requires task's phase start timestamp
        """
        return self._get_task_phase('start', task, phase)

    @_get_task_phase.used_events
    def df_rtapp_phase_end(self, task, phase=-1):
        """
        End of the specified phase for a given task.

        A negative phase value can be used to count from the oldest, e.g. -1
        represents the last phase.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :param phase: the ID of the phase end to return (default -1)
        :type phase: int

        :returns: the requires task's phase end timestamp
        """
        return self._get_task_phase('end', task, phase)

    @memoized
    @df_rtapp_task.used_events
    def task_window(self, task):
        """
        Return the start end end time for the specified task.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID
        """
        task_df = self.df_rtapp_task(task)
        start_time = task_df[task_df.event == "start"].index[0]
        end_time = task_df[task_df.event == "end"].index[0]

        return (start_time, end_time)

    @memoized
    @df_rtapp_phases_start.used_events
    def task_phase_window(self, task, phase):
        """
        Return the window of a requested task phase.

        For the specified ``task`` it returns a tuple with the (start, end)
        time of the requested ``phase``. A negative phase number can be used to
        count phases backward from the last (-1) toward the first.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :param phase: The ID of the phase to consider
        :type phase: int

        :rtype: PhaseWindow
        """
        phase_start = self.df_rtapp_phase_start(task, phase)
        phase_end = self.df_rtapp_phase_end(task, phase)

        return PhaseWindow(phase, phase_start, phase_end)

    @df_phases.used_events
    def task_phase_at(self, task, timestamp):
        """
        Return the :class:`PhaseWindow` for the specified
        task and timestamp.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :param timestamp: the timestamp to get the phase for
        :type timestamp: int or float

        :returns: the ID of the phase corresponding to the specified timestamp.
        """
        df = self.df_phases(task)

        def get_info(row):
            start = row.name
            end = start + row['duration']
            phase = row['phase']
            return (phase, start, end)

        _, _, last_phase_end = get_info(df.iloc[-1])
        if timestamp > last_phase_end:
            raise ValueError('timestamp={} is after last phase end: {}'.format(
                timestamp, last_phase_end))

        i = df.index.get_loc(timestamp, method='ffill')
        phase_id, phase_start, phase_end = get_info(df.iloc[i])
        return PhaseWindow(phase_id, phase_start, phase_end)

    ###########################################################################
    # rtapp_phase events related methods
    ###########################################################################

    @requires_events('rtapp_event')
    def df_rtapp_event(self, task=None):
        """
        Returns a :class:`pandas.DataFrame` of all the rt-app generated events.

        :param task: the (optional) rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :returns: a :class:`pandas.DataFrame` with:

          * A  ``__comm`` column: the actual rt-app trace task name
          * A  ``__pid``  column: the PID of the task
          * A ``__cpu``  column: the CPU on which the task was running at event
                                 generation time
          * A ``__line`` column: the ftrace line numer
          * A ``type`` column: the type of the generated event
          * A ``desc`` column: the mnemonic type of the generated event
          * A ``id`` column: the ID of the resource associated to the event,
                             e.g. the ID of the fired timer

        The ``index`` represents the timestamp of the event.
        """
        df = self.trace.df_events('rtapp_event')
        return self._task_filtered(df, task)

    ###########################################################################
    # rtapp_stats events related methods
    ###########################################################################

    @TraceAnalysisBase.cache
    @requires_events('rtapp_stats')
    def _get_stats(self):
        df = self.trace.df_events('rtapp_stats').copy(deep=True)
        # Add performance metrics column, performance is defined as:
        #             slack
        #   perf = -------------
        #          period - run
        df['perf_index'] = df['slack'] / (df['c_period'] - df['c_run'])

        return df

    @_get_stats.used_events
    def df_rtapp_stats(self, task=None):
        """
        Returns a :class:`pandas.DataFrame` of all the rt-app generated stats.

        :param task: the (optional) rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :returns: a :class:`pandas.DataFrame` with a set of colums representing
            the stats generated by rt-app after each loop.


        .. seealso:: the rt-app provided documentation:
            https://github.com/scheduler-tools/rt-app/blob/master/doc/tutorial.txt

            * A  ``__comm`` column: the actual rt-app trace task name
            * A  ``__pid``  column: the PID of the task
            * A ``__cpu``  column: the CPU on which the task was running at event
                                    generation time
            * A ``__line`` column: the ftrace line numer
            * A ``type`` column: the type of the generated event
            * A ``desc`` column: the mnemonic type of the generated event
            * A ``id`` column: the ID of the resource associated to the event,
                                e.g. the ID of the fired timer

        The ``index`` represents the timestamp of the event.
        """
        df = self._get_stats()
        return self._task_filtered(df, task)


###############################################################################
# Plotting Methods
###############################################################################

    @AnalysisHelpers.plot_method()
    @df_phases.used_events
    @may_use_events(TasksAnalysis.df_task_states.used_events)
    def plot_phases(self, task: TaskID, axis, local_fig):
        """
        Draw the task's phases colored bands

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID
        """
        phases_df = self.df_phases(task)

        try:
            states_df = self.trace.analysis.tasks.df_task_states(task)
        except MissingTraceEventError:
            def cpus_of_phase_at(t):
                return []
        else:
            def cpus_of_phase_at(t):
                end = t + phases_df['duration'][t]
                window = (t, end)
                df = df_window(states_df, window, method='pre')
                return sorted(int(x) for x in df['cpu'].unique())

        def make_band(row):
            t = row.name
            end = t + row['duration']
            phase = int(row['phase'])
            return (phase, t, end, cpus_of_phase_at(t))

        # Compute phases intervals
        bands = phases_df.apply(make_band, axis=1)

        for phase, start, end, cpus in bands:
            if cpus:
                cpus = ' (CPUs {})'.format(', '.join(map(str, cpus)))
            else:
                cpus = ''

            label = 'rt-app phase #{}{}'.format(phase, cpus)
            color = self.get_next_color(axis)
            axis.axvspan(start, end, alpha=0.1, facecolor=color, label=label)

        axis.legend(loc='upper center', bbox_to_anchor=(0.5, -0.2,), ncol=8)

        if local_fig:
            task = self.trace.get_task_id(task)
            axis.set_title("Task {} phases".format(task))

    @AnalysisHelpers.plot_method()
    @df_rtapp_stats.used_events
    def plot_perf(self, task: TaskID, axis, local_fig):
        r"""
        Plot the performance index.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        The perf index is defined as:

        .. math::

            perf_index = \frac{slack}{c_period - c_run}

        where

            - ``c_period``: is the configured period for an activation
            - ``c_run``: is the configured run time for an activation, assuming to
                        run at the maximum frequency and on the maximum capacity
                        CPU.
            - ``slack``: is the measured slack for an activation

        The slack is defined as the different among the activation deadline
        and the actual completion time of the activation.

        The deadline defines also the start of the next activation, thus in
        normal conditions a task activation is always required to complete
        before its deadline.

        The slack is thus a positive value if a task complete before its
        deadline. It's zero when a task complete an activation right at its
        eadline. It's negative when the completion is over the deadline.

        Thus, a performance index in [0..1] range represents activations
        completed within their deadlines. While, the more the performance index
        is negative the more the task is late with respect to its deadline.
        """
        task = self.trace.get_task_id(task)
        axis.set_title('Task {} Performance Index'.format(task))
        data = self.df_rtapp_stats(task)[['perf_index', ]]
        data.plot(ax=axis, drawstyle='steps-post')
        axis.set_ylim(0, 2)

    @AnalysisHelpers.plot_method()
    @df_rtapp_stats.used_events
    def plot_latency(self, task: TaskID, axis, local_fig):
        """
        Plot the Latency/Slack and Performance data for the specified task.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        .. seealso:: :meth:`plot_perf` for metrics definition.
        """
        task = self.trace.get_task_id(task)
        axis.set_title('Task {} (start) Latency and (completion) Slack'
                       .format(task))
        data = self.df_rtapp_stats(task)[['slack', 'wu_lat']]
        data.plot(ax=axis, drawstyle='steps-post')

    @AnalysisHelpers.plot_method()
    @df_rtapp_stats.used_events
    def plot_slack_histogram(self, task: TaskID, axis, local_fig, bins: int=30):
        """
        Plot the slack histogram.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :param bins: number of bins for the histogram.
        :type bins: int

        .. seealso:: :meth:`plot_perf` for the slack definition.
        """
        task = self.trace.get_task_id(task)
        ylabel = 'slack of "{}"'.format(task)
        series = self.df_rtapp_stats(task)['slack']
        series.hist(bins=bins, ax=axis, alpha=0.4, label=ylabel, figure=axis.get_figure())
        axis.axvline(series.mean(), linestyle='--', linewidth=2, label='mean')
        axis.legend()

        if local_fig:
            axis.set_title(ylabel)

    @AnalysisHelpers.plot_method()
    @df_rtapp_stats.used_events
    def plot_perf_index_histogram(self, task: TaskID, axis, local_fig, bins: int=30):
        """
        Plot the perf index histogram.

        :param task: the rt-app task to filter for
        :type task: int or str or lisa.trace.TaskID

        :param bins: number of bins for the histogram.
        :type bins: int

        .. seealso:: :meth:`plot_perf` for the perf index definition.

        """
        task = self.trace.get_task_id(task)
        ylabel = 'perf index of "{}"'.format(task)
        series = self.df_rtapp_stats(task)['perf_index']
        mean = series.mean()
        self.get_logger().info('perf index of task "{}": avg={:.2f} std={:.2f}'
                               .format(task, mean, series.std()))

        series.hist(bins=bins, ax=axis, alpha=0.4, label=ylabel, figure=axis.get_figure())
        axis.axvline(mean, linestyle='--', linewidth=2, label='mean')
        axis.legend()

        if local_fig:
            axis.set_title(ylabel)


@deprecate('Log-file based analysis has been replaced by ftrace-based analysis',
    deprecated_in='2.0',
    replaced_by=RTAEventsAnalysis,
)
class PerfAnalysis(AnalysisHelpers):
    """
    Parse and analyse a set of RTApp log files

    :param task_log_map: Mapping of task names to log files
    :type task_log_map: dict

    .. note:: That is not a subclass of
        :class:`lisa.analysis.base.TraceAnalysisBase` since it does not uses traces.
    """

    name = 'rta_logs'

    RTA_LOG_PATTERN = 'rt-app-{task}.log'
    "Filename pattern matching RTApp log files"

    def __init__(self, task_log_map):
        """
        Load peformance data of an rt-app workload
        """
        logger = self.get_logger()

        if not task_log_map:
            raise ValueError('No tasks in the task log mapping')

        for task_name, logfile in task_log_map.items():
            logger.debug('rt-app task [{}] logfile: {}'.format(
                task_name, logfile
            ))

        self.perf_data = {
            task_name: {
                'logfile': logfile,
                'df': self._parse_df(logfile),
            }
            for task_name, logfile in task_log_map.items()
        }

    @classmethod
    def from_log_files(cls, rta_logs):
        """
        Build a :class:`PerfAnalysis` from a sequence of RTApp log files

        :param rta_logs: sequence of path to log files
        :type rta_logs: list(str)
        """

        def find_task_name(logfile):
            logfile = os.path.basename(logfile)
            regex = cls.RTA_LOG_PATTERN.format(task=r'(.+)-[0-9]+')
            match = re.search(regex, logfile)
            if not match:
                raise ValueError('The logfile [{}] is not from rt-app'.format(logfile))
            return match.group(1)

        task_log_map = {
            find_task_name(logfile): logfile
            for logfile in rta_logs
        }
        return cls(task_log_map)

    @classmethod
    def from_dir(cls, log_dir):
        """
        Build a :class:`PerfAnalysis` from a folder path

        :param log_dir: Folder containing RTApp log files
        :type log_dir: str
        """
        rta_logs = glob.glob(os.path.join(
            log_dir, cls.RTA_LOG_PATTERN.format(task='*'),
        ))
        return cls.from_log_files(rta_logs)

    @classmethod
    def from_task_names(cls, task_names, log_dir):
        """
        Build a :class:`PerfAnalysis` from a list of task names

        :param task_names: List of task names to look for
        :type task_names: list(str)

        :param log_dir: Folder containing RTApp log files
        :type log_dir: str
        """
        def find_log_file(task_name, log_dir):
            log_file = os.path.join(log_dir, cls.RTA_LOG_PATTERN.format(task_name))
            if not os.path.isfile(log_file):
                raise ValueError('No rt-app logfile found for task [{}]'.format(
                    task_name
                ))
            return log_file

        task_log_map = {
            task_name: find_log_file(task_name, log_dir)
            for task_name in tasks_names
        }
        return cls(task_log_map)

    @staticmethod
    def _parse_df(logfile):
        df = pd.read_csv(logfile,
                sep=r'\s+',
                header=0,
                usecols=[1, 2, 3, 4, 7, 8, 9, 10],
                names=[
                    'Cycles', 'Run', 'Period', 'Timestamp',
                    'Slack', 'CRun', 'CPeriod', 'WKPLatency'
                ])
        # Normalize time to [s] with origin on the first event
        start_time = df['Timestamp'][0] / 1e6
        df['Time'] = df['Timestamp'] / 1e6 - start_time
        df.set_index(['Time'], inplace=True)
        # Add performance metrics column, performance is defined as:
        #             slack
        #   perf = -------------
        #          period - run
        df['PerfIndex'] = df['Slack'] / (df['CPeriod'] - df['CRun'])

        return df

    @property
    def tasks(self):
        """
        List of tasks for which performance data have been loaded
        """
        return sorted(self.perf_data.keys())

    def get_log_file(self, task):
        """
        Return the logfile for the specified task

        :param task: Name of the task that we want the logfile of.
        :type task: str
        """
        return self.perf_data[task]['logfile']

    def get_df(self, task):
        """
        Return the pandas dataframe with the performance data for the
        specified task

        :param task: Name of the task that we want the performance dataframe of.
        :type task: str
        """
        return self.perf_data[task]['df']

    def get_default_plot_path(self, **kwargs):
        # If all logfiles are located in the same folder, use that folder
        # and the default_filename
        dirnames = {
            os.path.realpath(os.path.dirname(perf_data['logfile']))
            for perf_data in self.perf_data.values()
        }
        if len(dirnames) != 1:
            raise ValueError('A default folder cannot be inferred from logfiles location unambiguously: {}'.format(dirnames))

        default_dir = dirnames.pop()

        return super().get_default_plot_path(
            default_dir=default_dir,
            **kwargs,
        )

    @AnalysisHelpers.plot_method()
    def plot_perf(self, task: TaskID, axis, local_fig):
        """
        Plot the performance Index
        """
        axis.set_title('Task {} Performance Index'.format(task))
        data = self.get_df(task)[['PerfIndex', ]]
        data.plot(ax=axis, drawstyle='steps-post')
        axis.set_ylim(0, 2)

    @AnalysisHelpers.plot_method()
    def plot_latency(self, task: TaskID, axis, local_fig):
        """
        Plot the Latency/Slack and Performance data for the specified task.
        """
        axis.set_title('Task {} (start) Latency and (completion) Slack'
                .format(task))
        data = self.get_df(task)[['Slack', 'WKPLatency']]
        data.plot(ax=axis, drawstyle='steps-post')

    @AnalysisHelpers.plot_method()
    def plot_slack_histogram(self, task: TaskID, axis, local_fig, bins: int=30):
        """
        Plot the slack histogram.

        :param task: rt-app task name to plot
        :type task: str

        :param bins: number of bins for the histogram.
        :type bins: int

        .. seealso:: :meth:`plot_perf_index_histogram`
        """
        ylabel = 'slack of "{}"'.format(task)
        series = self.get_df(task)['Slack']
        series.hist(bins=bins, ax=axis, alpha=0.4, label=ylabel)
        axis.axvline(series.mean(), linestyle='--', linewidth=2, label='mean')
        axis.legend()

        if local_fig:
            axis.set_title(ylabel)

    @AnalysisHelpers.plot_method()
    def plot_perf_index_histogram(self, task: TaskID, axis, local_fig, bins: int=30):
        r"""
        Plot the perf index histogram.

        :param task: rt-app task name to plot
        :type task: str

        :param bins: number of bins for the histogram.
        :type bins: int

        The perf index is defined as:

        .. math::

            perfIndex = \frac{slack}{period - runtime}

        """
        ylabel = 'perf index of {}'.format(task)
        series = self.get_df(task)['PerfIndex']
        mean = series.mean()
        self.get_logger().info('perf index of task "{}": avg={:.2f} std={:.2f}'.format(
            task, mean, series.std()))

        series.hist(bins=bins, ax=axis, alpha=0.4, label=ylabel)
        axis.axvline(mean, linestyle='--', linewidth=2, label='mean')
        axis.legend()

        if local_fig:
            axis.set_title(ylabel)

# vim :set tabstop=4 shiftwidth=4 textwidth=80 expandtab
