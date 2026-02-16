import time
from typing import Any
from dataclasses import dataclass


@dataclass
class HeapItem:
    priority: int
    timestamp: float
    sequence: int
    data: Any

    def __lt__(self, other):
        if self.priority != other.priority:
            return self.priority < other.priority
        return self.sequence < other.sequence


class PriorityHeap:
    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    AGING_THRESHOLD = 5.0

    def __init__(self):
        self.heap = []
        self.sequence = 0

    def _parent(self, i: int):
        return (i - 1) // 2

    def _left(self, i: int):
        return 2 * i + 1

    def _right(self, i: int):
        return 2 * i + 2

    def _swap(self, i: int, j: int):
        self.heap[i], self.heap[j] = self.heap[j], self.heap[i]

    def _bubble_up(self, i: int):
        while i > 0:
            parent = self._parent(i)
            if self.heap[i] < self.heap[parent]:
                self._swap(i, parent)
                i = parent
            else:
                break

    def _bubble_down(self, i: int):
        size = len(self.heap)
        while True:
            smallest = i
            left = self._left(i)
            right = self._right(i)

            if left < size and self.heap[left] < self.heap[smallest]:
                smallest = left
            if right < size and self.heap[right] < self.heap[smallest]:
                smallest = right

            if smallest != i:
                self._swap(i, smallest)
                i = smallest
            else:
                break

    def insert(self, data: Any, priority: int = NORMAL):
        item = HeapItem(
            priority=priority,
            timestamp=time.time(),
            sequence=self.sequence,
            data=data
        )
        self.sequence += 1
        self.heap.append(item)
        self._bubble_up(len(self.heap) - 1)

    def extract_min(self):
        if not self.heap:
            return None

        if len(self.heap) == 1:
            return self.heap.pop().data

        min_item = self.heap[0]
        self.heap[0] = self.heap.pop()
        self._bubble_down(0)

        return min_item.data

    def peek(self):
        return self.heap[0].data if self.heap else None

    def size(self):
        return len(self.heap)

    def is_empty(self):
        return len(self.heap) == 0

    def decrease_key(self, index: int, new_priority: int):
        """Decrease the priority value of an item at the given index (lower = higher priority).
        Maintains heap invariant in O(log n) time."""
        if index < 0 or index >= len(self.heap):
            return
        if new_priority >= self.heap[index].priority:
            return  # Only allow decreasing the priority value
        self.heap[index].priority = new_priority
        self._bubble_up(index)

    def apply_aging(self):
        """Boost priority of items waiting longer than AGING_THRESHOLD.
        Uses decrease_key for O(log n) per boosted item."""
        current_time = time.time()

        for i in range(len(self.heap)):
            item = self.heap[i]
            wait_time = current_time - item.timestamp
            if wait_time > self.AGING_THRESHOLD and item.priority > self.CRITICAL:
                new_priority = item.priority - 1
                self.decrease_key(i, new_priority)
