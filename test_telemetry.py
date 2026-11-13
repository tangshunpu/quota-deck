import unittest
from unittest.mock import patch
import subprocess
from telemetry import cpu_counters,cpu_percent,memory_usage,gpu_usage,SystemMonitor

class TelemetryTests(unittest.TestCase):
    def test_cpu_does_not_count_guest_twice(self):
        self.assertEqual(cpu_counters('cpu 100 0 20 300 10 5 5 0 40 0\n'),(440,310))

    def test_cpu_uses_interval_and_initial_value_is_unknown(self):
        self.assertIsNone(cpu_percent(None,(440,310)))
        self.assertEqual(cpu_percent((440,310),(540,370)),40)
        self.assertIsNone(cpu_percent((440,310),(400,300)))

    def test_cpu_idle_is_zero(self):
        self.assertEqual(cpu_percent((100,80),(200,180)),0)

    def test_memory_uses_available_including_reclaimable_cache(self):
        result=memory_usage('MemTotal: 1000 kB\nMemFree: 50 kB\nMemAvailable: 750 kB\nCached: 700 kB\n')
        self.assertEqual(result['percent'],25)
        self.assertEqual(result['usedBytes'],250*1024)

    def test_missing_memory_is_not_zero(self):
        self.assertIsNone(memory_usage('MemTotal: 1000 kB\n'))

    def test_gpu_reports_utilization_separately_from_vram(self):
        result=gpu_usage('0, Test GPU, 40, 100, 1000\n1, Second GPU, 0, 0, 2000\n')
        self.assertEqual(result[0]['percent'],40)
        self.assertEqual(result[0]['usedBytes'],100*1024**2)
        self.assertEqual(result[1]['percent'],0)

    def test_gpu_unavailable_is_not_zero(self):
        result=gpu_usage('0, Test GPU, [N/A], [N/A], 1000\n')
        self.assertIsNone(result[0]['percent'])
        self.assertIsNone(result[0]['usedBytes'])

    def test_gpu_timeout_does_not_block_cpu_memory(self):
        monitor=SystemMonitor()
        monitor.gpu_command='nvidia-smi'
        with patch('telemetry.subprocess.run',side_effect=subprocess.TimeoutExpired('nvidia-smi',1.5)):
            monitor.sample()
        state=monitor.snapshot()
        self.assertEqual(state['gpuStatus'],'error')
        self.assertEqual(state['gpus'],[])
        self.assertIsNotNone(state['memory'])

if __name__=='__main__':unittest.main()
