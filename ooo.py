import sys
import os
import random
import time
from collections import Counter
import math

# --- Platform-specific imports for file sharing ---
if sys.platform == "win32":
    import win32file
    import win32con
    import msvcrt

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QFileDialog, QFrame, QLabel, QProgressBar, QDialog,
    QSpinBox, QFormLayout, QDialogButtonBox, QGraphicsView, QGraphicsScene,
    QGraphicsPixmapItem, QScrollArea, QGridLayout
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QRectF, QTimer, QSize
)
from PyQt6.QtGui import QColor, QPainter, QBrush, QPen, QFont, QImage, QPixmap, QIcon

# --- 全局样式与配置 ---
class AppConfig:
    COLOR_BACKGROUND = QColor("#1D1616")
    COLOR_PRIMARY = QColor("#EEEEEE")
    
    GRADIENT_START = QColor("#8E1616")
    GRADIENT_END = QColor("#FF6363")
    COLOR_GLOW_START = QColor(Qt.GlobalColor.white)
    ANIMATION_DURATION_S = 0.6
    
    WINDOW_WIDTH = 1200
    WINDOW_HEIGHT = 400
    
    VIS_WIDTH = 800
    VIS_HEIGHT = 80
    CELL_SIZE = 2
    LOGICAL_WIDTH = VIS_WIDTH // CELL_SIZE
    LOGICAL_HEIGHT = VIS_HEIGHT // CELL_SIZE
    TOTAL_POINTS = LOGICAL_WIDTH * LOGICAL_HEIGHT
    
    NUM_WORKERS = os.cpu_count() or 4
    BYTES_PER_SAMPLE = 8
    
    # 文件集配置
    GRID_CELL_LOGICAL_WIDTH = 100
    GRID_CELL_LOGICAL_HEIGHT = 100
    GRID_TOTAL_POINTS = GRID_CELL_LOGICAL_WIDTH * GRID_CELL_LOGICAL_HEIGHT
    GRID_MAX_COLS = 6
    GRID_FILENAME_MAX_LEN = 12
    
    # --- 性能控制参数 ---
    SINGLE_FILE_DELAY_MS = 1
    FILESET_BATCH_SIZE = 500 
    FILESET_TIMER_MS = 50 

    STYLESHEET = f"""
        QDialog, QMainWindow {{
            background-color: {COLOR_BACKGROUND.name()};
        }}
        QWidget {{
            background-color: {COLOR_BACKGROUND.name()};
            color: {COLOR_PRIMARY.name()};
            font-family: "Segoe UI", "Microsoft YaHei";
            font-size: 14px;
        }}
        QPushButton {{
            background-color: #D84040;
            border: none;
            padding: 10px 20px;
            border-radius: 15px;
            font-weight: bold;
        }}
        QPushButton:hover {{ background-color: {GRADIENT_END.name()}; }}
        QPushButton:pressed {{ background-color: {GRADIENT_START.name()}; }}
        QLabel#TitleLabel, QLabel#DetailsTitleLabel {{
            font-size: 18px;
            font-weight: bold;
        }}
        QLabel#StatusLabel, QLabel#InfoLabel, QLabel#FileNameLabel {{ color: #AAAAAA; }}
        QProgressBar {{
            border: 2px solid {GRADIENT_START.name()};
            border-radius: 10px;
            text-align: center;
            background-color: {COLOR_BACKGROUND.name()};
        }}
        QProgressBar::chunk {{
            background-color: #D84040;
            border-radius: 8px;
        }}
        QLabel.SampleLabel {{
            font-family: "Consolas", "Courier New";
            font-size: 12px;
            color: #CCCCCC;
            background-color: #2a2a2a;
            padding: 2px 5px;
            border-radius: 4px;
        }}
        QSpinBox {{
            padding: 5px;
            border: 1px solid #555;
            border-radius: 5px;
        }}
        QScrollArea {{
            border: none;
        }}
        QGraphicsView {{
            border-style: none;
        }}
    """

# --- Windows-specific shared open function ---
def open_file_for_shared_read(filepath):
    if sys.platform == "win32":
        try:
            handle = win32file.CreateFile(
                filepath, win32con.GENERIC_READ,
                win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE | win32con.FILE_SHARE_DELETE,
                None, win32con.OPEN_EXISTING, win32con.FILE_ATTRIBUTE_NORMAL, None
            )
            fd = msvcrt.open_osfhandle(handle.Detach(), os.O_RDONLY)
            return os.fdopen(fd, 'rb')
        except Exception: return None
    else:
        return open(filepath, 'rb')

# --- 渲染工人 (主界面) ---
class RenderWorker(QThread):
    point_sampled = pyqtSignal(int, int, int)
    initial_pass_finished = pyqtSignal()

    def __init__(self, file_path, initial_coords, chunk_size, logical_height, is_persistent=False):
        super().__init__()
        self.file_path = file_path
        self.initial_coords = initial_coords
        self.chunk_size = chunk_size
        self.logical_height = logical_height
        self.is_persistent = is_persistent
        self.is_running = True

    def sample_point(self, f, x, y):
        index = x * self.logical_height + y
        region_start = int(index * self.chunk_size)
        max_offset = max(0, self.chunk_size - AppConfig.BYTES_PER_SAMPLE)
        sample_pos = region_start + random.randint(0, int(max_offset))
        f.seek(sample_pos)
        data = f.read(AppConfig.BYTES_PER_SAMPLE)
        if data:
            score = len(set(data))
            self.point_sampled.emit(x, y, score)

    def run(self):
        try:
            with open_file_for_shared_read(self.file_path) as f:
                if f is None: return
                
                for x, y in self.initial_coords:
                    if not self.is_running: return
                    self.sample_point(f, x, y)
                
                if self.is_persistent:
                    self.initial_pass_finished.emit()
                    while self.is_running:
                        rand_x = random.randint(0, AppConfig.LOGICAL_WIDTH - 1)
                        rand_y = random.randint(0, AppConfig.LOGICAL_HEIGHT - 1)
                        self.sample_point(f, rand_x, rand_y)
                        self.msleep(AppConfig.SINGLE_FILE_DELAY_MS)

        except Exception:
            pass

    def stop(self):
        self.is_running = False

# --- 主工作线程 (单文件) ---
class FileProcessorThread(QThread):
    point_sampled = pyqtSignal(int, int, int)
    first_pass_fully_finished = pyqtSignal()
    progress_updated = pyqtSignal(int)
    error_occurred = pyqtSignal(str)

    def __init__(self, file_path, is_persistent=True):
        super().__init__()
        self.file_path = file_path
        self.is_persistent = is_persistent
        self.is_running = True
        self.workers = []
        self.points_processed = 0
        self.finished_workers_count = 0

    def run(self):
        try:
            if not os.path.exists(self.file_path):
                self.error_occurred.emit("文件不存在")
                return
            
            file_size = os.path.getsize(self.file_path)
            if file_size < AppConfig.TOTAL_POINTS:
                self.error_occurred.emit("文件太小，无法映射")
                return

            chunk_size = file_size / AppConfig.TOTAL_POINTS
            all_coordinates = [(x, y) for y in range(AppConfig.LOGICAL_HEIGHT) for x in range(AppConfig.LOGICAL_WIDTH)]
            random.shuffle(all_coordinates)
            
            coords_per_worker = (AppConfig.TOTAL_POINTS + AppConfig.NUM_WORKERS - 1) // AppConfig.NUM_WORKERS
            coord_chunks = [all_coordinates[i:i + coords_per_worker] for i in range(0, AppConfig.TOTAL_POINTS, coords_per_worker)]

            for chunk in coord_chunks:
                if not self.is_running: return
                worker = RenderWorker(self.file_path, chunk, chunk_size, AppConfig.LOGICAL_HEIGHT, self.is_persistent)
                worker.point_sampled.connect(self.handle_sample)
                if self.is_persistent:
                    worker.initial_pass_finished.connect(self.handle_worker_finished_initial_pass)
                self.workers.append(worker)
                worker.start()
            
            if not self.is_persistent:
                for worker in self.workers:
                    worker.wait()
                self.first_pass_fully_finished.emit()

        except Exception as e:
            self.error_occurred.emit(f"处理文件时出错: {e}")

    def handle_sample(self, x, y, score):
        if self.finished_workers_count < len(self.workers):
            self.points_processed += 1
            progress = int(self.points_processed / AppConfig.TOTAL_POINTS * 100)
            self.progress_updated.emit(progress)
        self.point_sampled.emit(x, y, score)

    def handle_worker_finished_initial_pass(self):
        self.finished_workers_count += 1
        if self.finished_workers_count == len(self.workers):
            self.first_pass_fully_finished.emit()

    def stop(self):
        self.is_running = False
        for worker in self.workers:
            worker.stop()
            worker.wait()

# --- 可视化区域控件 (主窗口和文件集通用) ---
class VisualizationWidget(QWidget):
    def __init__(self, logical_width, logical_height):
        super().__init__()
        self.logical_width = logical_width
        self.logical_height = logical_height
        self.setFixedSize(logical_width * AppConfig.CELL_SIZE, logical_height * AppConfig.CELL_SIZE)
        
        self.grid_stats = {}
        self.active_glows = {}
        self.animation_timer = QTimer(self)
        self.animation_timer.timeout.connect(self._tick_animations)
        self.animation_timer.start(16)

    def _interpolate_color(self, start_color, end_color, t):
        r = int(start_color.red() * (1 - t) + end_color.red() * t)
        g = int(start_color.green() * (1 - t) + end_color.green() * t)
        b = int(start_color.blue() * (1 - t) + end_color.blue() * t)
        return QColor(r, g, b)

    def _tick_animations(self):
        if not self.active_glows: return
        current_time = time.monotonic()
        finished_glows = []
        for pos, data in self.active_glows.items():
            elapsed = current_time - data['start_time']
            t = min(1.0, elapsed / AppConfig.ANIMATION_DURATION_S)
            current_color = self._interpolate_color(AppConfig.COLOR_GLOW_START, data['end_color'], t)
            if pos in self.grid_stats: self.grid_stats[pos]['display_color'] = current_color
            if t >= 1.0: finished_glows.append(pos)
        for pos in finished_glows: del self.active_glows[pos]
        self.update()

    def get_color_for_score(self, score):
        t = (score - 1.0) / 7.0
        return self._interpolate_color(AppConfig.GRADIENT_START, AppConfig.GRADIENT_END, t)

    def clear_grid(self):
        self.active_glows.clear()
        self.grid_stats.clear()
        self.update()

    def update_point_average(self, x, y, new_score):
        pos = (x, y)
        if pos not in self.grid_stats:
            self.grid_stats[pos] = {'total_score': 0, 'count': 0, 'display_color': AppConfig.COLOR_BACKGROUND}
        stats = self.grid_stats[pos]
        new_count = stats['count'] + 1
        new_total = stats['total_score'] + new_score
        new_average_score = new_total / new_count
        stats['total_score'] = new_total
        stats['count'] = new_count
        new_color = self.get_color_for_score(new_average_score)
        if new_color != stats['display_color']:
            self.active_glows[pos] = {'start_time': time.monotonic(), 'end_color': new_color}

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.grid_stats:
            painter.fillRect(self.rect(), AppConfig.COLOR_BACKGROUND)
            return
        cell_size = AppConfig.CELL_SIZE
        for (x, y), stats in self.grid_stats.items():
            painter.setBrush(QBrush(stats['display_color']))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRect(QRectF(x * cell_size, y * cell_size, cell_size, cell_size))

# --- 文件集浏览器相关 ---
class FileGridWidget(QWidget):
    def __init__(self, file_path):
        super().__init__()
        self.vis_widget = VisualizationWidget(AppConfig.GRID_CELL_LOGICAL_WIDTH, AppConfig.GRID_CELL_LOGICAL_HEIGHT)
        
        self.filename_label = QLabel()
        self.filename_label.setObjectName("FileNameLabel")
        self.filename_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        
        base_name = os.path.basename(file_path)
        if len(base_name) > AppConfig.GRID_FILENAME_MAX_LEN:
            elided_name = base_name[:AppConfig.GRID_FILENAME_MAX_LEN] + "..."
        else:
            elided_name = base_name
        self.filename_label.setText(elided_name)
        
        layout = QVBoxLayout(self)
        layout.addWidget(self.vis_widget)
        layout.addWidget(self.filename_label)

    def update_pixel(self, x, y, score):
        self.vis_widget.update_point_average(x, y, score)

class FileSetBatchWorker(QThread):
    point_sampled = pyqtSignal(str, int, int, int)

    def __init__(self, samples_to_process):
        super().__init__()
        self.samples_to_process = samples_to_process
        self.is_running = True

    def run(self):
        try:
            for file_path in Counter(s[0] for s in self.samples_to_process):
                with open_file_for_shared_read(file_path) as f:
                    if f is None: continue
                    
                    file_size = os.fstat(f.fileno()).st_size
                    chunk_size = file_size / AppConfig.GRID_TOTAL_POINTS
                    
                    for path, x, y in self.samples_to_process:
                        if path != file_path or not self.is_running: continue
                        
                        index = x * AppConfig.GRID_CELL_LOGICAL_HEIGHT + y
                        region_start = int(index * chunk_size)
                        max_offset = max(0, chunk_size - AppConfig.BYTES_PER_SAMPLE)
                        sample_pos = region_start + random.randint(0, int(max_offset))
                        
                        f.seek(sample_pos)
                        data = f.read(AppConfig.BYTES_PER_SAMPLE)
                        if data:
                            score = len(set(data))
                            self.point_sampled.emit(file_path, x, y, score)
        except Exception:
            pass
            
    def stop(self):
        self.is_running = False

class FileSetProcessorThread(QThread):
    file_discovered = pyqtSignal(str)
    
    def __init__(self, folder_path):
        super().__init__()
        self.folder_path = folder_path
        self.is_running = True

    def run(self):
        try:
            for entry in os.scandir(self.folder_path):
                if not self.is_running: return
                min_size = AppConfig.GRID_TOTAL_POINTS * AppConfig.BYTES_PER_SAMPLE
                if entry.is_file() and entry.stat().st_size >= min_size:
                    self.file_discovered.emit(entry.path)
        except Exception:
            pass

    def stop(self):
        self.is_running = False

class FileSetWindow(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文件密度可视化 - 文件集浏览器")
        self.setMinimumSize(1024, 768)
        
        self.processor_thread = None
        self.file_widgets = {}
        self.file_counter = 0
        self.render_states = {}
        self.active_batch_workers = []
        
        self.sampling_timer = QTimer(self)
        self.sampling_timer.timeout.connect(self.trigger_sampling_batch)
        
        main_layout = QVBoxLayout(self)
        
        top_bar = QHBoxLayout()
        select_folder_button = QPushButton("选择文件夹...")
        select_folder_button.clicked.connect(self.select_folder)
        self.status_label = QLabel("请选择一个文件夹。")
        self.status_label.setObjectName("StatusLabel")
        top_bar.addWidget(select_folder_button)
        top_bar.addWidget(self.status_label, 1)
        main_layout.addLayout(top_bar)
        
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        
        self.grid_container = QWidget()
        self.grid_layout = QGridLayout(self.grid_container)
        self.grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)

        scroll_area.setWidget(self.grid_container)
        main_layout.addWidget(scroll_area)

    def select_folder(self, folder_path=None):
        if not folder_path:
            folder_path = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if folder_path:
            self.start_processing(folder_path)

    def start_processing(self, folder_path):
        self.stop_all_threads()
        
        for i in reversed(range(self.grid_layout.count())): 
            item = self.grid_layout.itemAt(i)
            if item:
                widget = item.widget()
                if widget: widget.setParent(None)

        self.file_widgets.clear()
        self.file_counter = 0
        self.render_states.clear()
        self.status_label.setText(f"扫描文件夹: {folder_path}")
        
        self.processor_thread = FileSetProcessorThread(folder_path)
        self.processor_thread.file_discovered.connect(self.add_file_widget)
        self.processor_thread.finished.connect(self.start_sampling)
        self.processor_thread.start()

    def add_file_widget(self, file_path):
        widget = FileGridWidget(file_path)
        row = self.file_counter // AppConfig.GRID_MAX_COLS
        col = self.file_counter % AppConfig.GRID_MAX_COLS
        self.grid_layout.addWidget(widget, row, col)

        self.file_widgets[file_path] = widget
        
        coords = [(x, y) for y in range(AppConfig.GRID_CELL_LOGICAL_HEIGHT) for x in range(AppConfig.GRID_CELL_LOGICAL_WIDTH)]
        random.shuffle(coords)
        self.render_states[file_path] = {
            'shuffled_coords': coords,
            'current_index': 0
        }

        self.file_counter += 1
        self.status_label.setText(f"已发现 {self.file_counter} 个有效文件...")

    def start_sampling(self):
        if self.render_states:
            self.status_label.setText(f"开始精炼 {len(self.render_states)} 个文件...")
            self.sampling_timer.start(AppConfig.FILESET_TIMER_MS)
        else:
            self.status_label.setText("未发现足够大的文件。")

    def trigger_sampling_batch(self):
        if not self.render_states:
            self.sampling_timer.stop()
            return
        
        finished_workers = [w for w in self.active_batch_workers if not w.isRunning()]
        for worker in finished_workers:
            self.active_batch_workers.remove(worker)

        samples_to_process = []
        
        if not self.render_states: return
        points_per_file_per_batch = max(1, AppConfig.FILESET_BATCH_SIZE // len(self.render_states))

        for file_path, state in self.render_states.items():
            start_index = state['current_index']
            end_index = start_index + points_per_file_per_batch
            
            chunk = state['shuffled_coords'][start_index:end_index]
            samples_to_process.extend([(file_path, x, y) for x, y in chunk])
            
            if end_index >= len(state['shuffled_coords']):
                state['current_index'] = 0
                random.shuffle(state['shuffled_coords'])
            else:
                state['current_index'] = end_index

        coords_per_worker = (len(samples_to_process) + AppConfig.NUM_WORKERS - 1) // AppConfig.NUM_WORKERS
        
        for i in range(AppConfig.NUM_WORKERS):
            chunk = samples_to_process[i * coords_per_worker: (i + 1) * coords_per_worker]
            if not chunk: continue
            
            worker = FileSetBatchWorker(chunk)
            worker.point_sampled.connect(self.update_file_pixel)
            worker.start()
            self.active_batch_workers.append(worker)

    def update_file_pixel(self, file_path, x, y, score):
        if file_path in self.file_widgets:
            self.file_widgets[file_path].update_pixel(x, y, score)

    def stop_all_threads(self):
        self.sampling_timer.stop()
        if self.processor_thread and self.processor_thread.isRunning():
            self.processor_thread.stop()
            self.processor_thread.wait()
        
        for worker in self.active_batch_workers:
            if worker.isRunning():
                worker.stop()
                worker.wait()
        self.active_batch_workers.clear()

    def closeEvent(self, event):
        self.stop_all_threads()
        event.accept()

# --- 导出功能窗口 ---
class ExportDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("导出配置 - 文件密度可视化")
        self.file_path = ""
        self.setMinimumWidth(400)

        layout = QFormLayout(self)
        
        self.width_spin = QSpinBox(); self.width_spin.setRange(1, 16384); self.width_spin.setValue(1920)
        self.height_spin = QSpinBox(); self.height_spin.setRange(1, 16384); self.height_spin.setValue(1080)
        self.bytes_spin = QSpinBox(); self.bytes_spin.setRange(1, 64); self.bytes_spin.setValue(8)
        
        self.file_label = QLabel("未选择文件"); self.file_label.setObjectName("InfoLabel")
        file_button = QPushButton("选择文件...")
        self.estimate_label = QLabel("N/A")
        
        layout.addRow("宽度 (px):", self.width_spin)
        layout.addRow("高度 (px):", self.height_spin)
        layout.addRow("采样字节:", self.bytes_spin)
        layout.addRow("源文件:", self.file_label)
        layout.addRow("", file_button)
        layout.addRow("最小文件大小:", self.estimate_label)
        
        self.button_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setText("开始渲染")
        self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        layout.addRow(self.button_box)

        self.width_spin.valueChanged.connect(self._update_ui)
        self.height_spin.valueChanged.connect(self._update_ui)
        self.bytes_spin.valueChanged.connect(self._update_ui)
        file_button.clicked.connect(self._select_file)
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        self._update_ui()

    def _select_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择源文件")
        if path:
            self.file_path = path
            self.file_label.setText(os.path.basename(path))
            self._update_ui()

    def _update_ui(self):
        w, h, b = self.width_spin.value(), self.height_spin.value(), self.bytes_spin.value()
        min_size = w * h * b
        if min_size < 1024**2: self.estimate_label.setText(f"{min_size/1024:.2f} KB")
        elif min_size < 1024**3: self.estimate_label.setText(f"{min_size/1024**2:.2f} MB")
        else: self.estimate_label.setText(f"{min_size/1024**3:.2f} GB")
        
        if self.file_path:
            file_size = os.path.getsize(self.file_path)
            if file_size >= min_size:
                self.estimate_label.setStyleSheet("color: #4CAF50;")
                self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
            else:
                self.estimate_label.setStyleSheet("color: #F44336;")
                self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        else:
            self.button_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def get_config(self):
        return {"width": self.width_spin.value(), "height": self.height_spin.value(), "bytes": self.bytes_spin.value(), "file_path": self.file_path}

class RenderWindow(QDialog):
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.config = config
        self.setWindowTitle(f"导出渲染 - 文件密度可视化")
        self.setMinimumSize(800, 600)
        
        self.image = QImage(config['width'], config['height'], QImage.Format.Format_RGB32)
        self.image.fill(AppConfig.COLOR_BACKGROUND)
        
        self.scene = QGraphicsScene()
        self.pixmap_item = QGraphicsPixmapItem(QPixmap.fromImage(self.image))
        self.scene.addItem(self.pixmap_item)
        
        self.view = QGraphicsView(self.scene)
        self.view.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.view.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat("渲染中: %p%")
        
        layout = QVBoxLayout(self)
        layout.addWidget(self.view)
        layout.addWidget(self.progress_bar)
        
        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self.update_pixmap)
        self.update_timer.start(100)
        
        self.start_render()

    def wheelEvent(self, event):
        factor = 1.2 if event.angleDelta().y() > 0 else 1 / 1.2
        self.view.scale(factor, factor)

    def start_render(self):
        self.render_thread = ExportRenderThread(self.config)
        self.render_thread.point_rendered.connect(self.update_pixel)
        self.render_thread.progress_updated.connect(self.progress_bar.setValue)
        self.render_thread.finished.connect(self.on_render_finished)
        self.render_thread.start()

    def update_pixel(self, x, y, color):
        self.image.setPixelColor(x, y, color)

    def _interpolate_color(self, start_color, end_color, t):
        r = int(start_color.red() * (1 - t) + end_color.red() * t)
        g = int(start_color.green() * (1 - t) + end_color.green() * t)
        b = int(start_color.blue() * (1 - t) + end_color.blue() * t)
        return QColor(r, g, b)

    def update_pixmap(self):
        self.pixmap_item.setPixmap(QPixmap.fromImage(self.image))

    def on_render_finished(self):
        self.update_timer.stop()
        self.update_pixmap()
        self.progress_bar.setVisible(False)
        
        file_name, _ = QFileDialog.getSaveFileName(self, "保存图片", "", "PNG Images (*.png)")
        if file_name:
            self.image.save(file_name)
        self.close()

class ExportRenderThread(QThread):
    point_rendered = pyqtSignal(int, int, QColor)
    progress_updated = pyqtSignal(int)

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.points_processed = 0

    def run(self):
        w, h, b, path = self.config['width'], self.config['height'], self.config['bytes'], self.config['file_path']
        total_points = w * h
        file_size = os.path.getsize(path)
        chunk_size = file_size / total_points
        
        with open_file_for_shared_read(path) as f:
            for y in range(h):
                for x in range(w):
                    index = x * h + y
                    region_start = int(index * chunk_size)
                    max_offset = max(0, chunk_size - b)
                    sample_pos = region_start + random.randint(0, int(max_offset))
                    f.seek(sample_pos)
                    data = f.read(b)
                    if data:
                        score = len(set(data))
                        t = (score - 1.0) / (b - 1.0) if b > 1 else 0.5
                        color = self._interpolate_color(AppConfig.GRADIENT_START, AppConfig.GRADIENT_END, t)
                        self.point_rendered.emit(x, y, color)
            
                    self.points_processed += 1
                    progress = int(self.points_processed / total_points * 100)
                    self.progress_updated.emit(progress)
    
    def _interpolate_color(self, start_color, end_color, t):
        r = int(start_color.red() * (1 - t) + end_color.red() * t)
        g = int(start_color.green() * (1 - t) + end_color.green() * t)
        b = int(start_color.blue() * (1 - t) + end_color.blue() * t)
        return QColor(r, g, b)

# --- 主窗口 ---
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("文件密度可视化")
        self.setWindowIcon(QIcon("icon.ico"))
        self.setFixedSize(AppConfig.WINDOW_WIDTH, AppConfig.WINDOW_HEIGHT)
        
        self.file_path = None
        self.render_thread = None
        self.file_set_window = None
        self.init_ui()

    def init_ui(self):
        main_widget = QWidget()
        main_layout = QHBoxLayout(main_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)
        self.setCentralWidget(main_widget)

        left_layout = QVBoxLayout()
        left_layout.setSpacing(15)
        title_label = QLabel("文件密度可视化")
        title_label.setObjectName("TitleLabel")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.vis_widget = VisualizationWidget(AppConfig.LOGICAL_WIDTH, AppConfig.LOGICAL_HEIGHT)
        self.vis_widget.mousePressEvent = self.show_sample_details
        control_layout = QHBoxLayout()
        self.select_button = QPushButton("选择文件")
        self.select_button.clicked.connect(self.select_file)
        self.status_label = QLabel("请选择一个文件进行渲染。")
        self.status_label.setObjectName("StatusLabel")
        control_layout.addWidget(self.select_button)
        control_layout.addWidget(self.status_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        self.progress_bar.setFormat("第一遍渲染: %p%")
        left_layout.addWidget(title_label)
        left_layout.addWidget(self.vis_widget, 0, Qt.AlignmentFlag.AlignCenter)
        left_layout.addLayout(control_layout)
        left_layout.addWidget(self.progress_bar)
        left_layout.addStretch()

        right_layout = QVBoxLayout()
        right_layout.setSpacing(5)
        details_title = QLabel("像素点详情")
        details_title.setObjectName("DetailsTitleLabel")
        self.details_coord_label = QLabel("点击左侧区域查看...")
        self.details_coord_label.setObjectName("StatusLabel")
        self.details_stats_label = QLabel("采样次数: N/A | 平均熵: N/A")
        self.details_stats_label.setObjectName("StatusLabel")
        right_layout.addWidget(details_title)
        right_layout.addWidget(self.details_coord_label)
        right_layout.addWidget(self.details_stats_label)
        self.sample_labels = []
        for i in range(1):
            label = QLabel("新样本: N/A")
            label.setObjectName("SampleLabel")
            self.sample_labels.append(label)
            right_layout.addWidget(label)
        
        right_layout.addStretch()
        export_button = QPushButton("导出图片...")
        export_button.clicked.connect(self.open_export_dialog)
        fileset_button = QPushButton("文件集采样...")
        fileset_button.clicked.connect(self.open_fileset_window)
        right_layout.addWidget(export_button)
        right_layout.addWidget(fileset_button)

        main_layout.addLayout(left_layout, 7)
        main_layout.addLayout(right_layout, 3)

    def open_export_dialog(self):
        dialog = ExportDialog(self)
        if dialog.exec():
            config = dialog.get_config()
            render_window = RenderWindow(config, self)
            render_window.exec()

    def open_fileset_window(self):
        if not self.file_set_window:
            self.file_set_window = FileSetWindow(self)
        self.file_set_window.show()
        self.file_set_window.activateWindow()

    def select_file(self):
        self.stop_all_threads()
        file_path, _ = QFileDialog.getOpenFileName(self, "选择一个文件")
        if file_path:
            self.file_path = file_path
            self.status_label.setText(f"渲染中: {os.path.basename(self.file_path)}")
            self.start_processing()

    def start_processing(self):
        self.vis_widget.clear_grid()
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(True)
        self.select_button.setEnabled(False)

        self.render_thread = FileProcessorThread(self.file_path)
        self.render_thread.point_sampled.connect(self.vis_widget.update_point_average)
        self.render_thread.progress_updated.connect(self.progress_bar.setValue)
        self.render_thread.first_pass_fully_finished.connect(self.on_first_pass_finished)
        self.render_thread.error_occurred.connect(self.on_error)
        self.render_thread.start()

    def on_first_pass_finished(self):
        self.progress_bar.setVisible(False)
        self.select_button.setEnabled(True)
        self.status_label.setText(f"持续精炼中: {os.path.basename(self.file_path)}")

    def on_error(self, message):
        self.status_label.setText(f"错误: {message}")
        self.progress_bar.setVisible(False)
        self.select_button.setEnabled(True)

    def show_sample_details(self, event):
        if not self.file_path: return
        x = int(event.position().x() / AppConfig.CELL_SIZE)
        y = int(event.position().y() / AppConfig.CELL_SIZE)
        
        pos = (x, y)
        stats = self.vis_widget.grid_stats.get(pos)
        if stats:
            avg_score = stats['total_score'] / stats['count']
            self.details_stats_label.setText(f"采样次数: {stats['count']} | 平均熵: {avg_score:.2f}")
        else:
            self.details_stats_label.setText("采样次数: 0 | 平均熵: N/A")
        file_size = os.path.getsize(self.file_path)
        chunk_size = file_size / AppConfig.TOTAL_POINTS
        index = x * AppConfig.LOGICAL_HEIGHT + y
        region_start = int(index * chunk_size)
        self.details_coord_label.setText(f"坐标: ({x}, {y}), 文件区域: {region_start}")
        try:
            with open_file_for_shared_read(self.file_path) as f:
                max_offset_in_region = max(0, chunk_size - AppConfig.BYTES_PER_SAMPLE)
                random_offset = random.randint(0, int(max_offset_in_region))
                f.seek(region_start + random_offset)
                sample_data = f.read(AppConfig.BYTES_PER_SAMPLE)
                hex_data = sample_data.hex().upper()
                self.sample_labels[0].setText(f"新样本: {hex_data}")
        except Exception as e:
            self.details_coord_label.setText(f"无法读取文件详情: {e}")

    def stop_all_threads(self):
        if self.render_thread and self.render_thread.isRunning():
            self.render_thread.stop()
            self.render_thread.wait()

    def closeEvent(self, event):
        self.stop_all_threads()
        if self.file_set_window:
            self.file_set_window.close()
        event.accept()

# --- 程序入口 ---
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyleSheet(AppConfig.STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())