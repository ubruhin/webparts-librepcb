
import logging
import os
import sys
from functools import partial
from typing import List

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QCursor, QPixmap
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGraphicsView,
    QGraphicsScene,
    QGraphicsPixmapItem,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from models.search_result import SearchResult
from search import Search
from search_page import SearchPage
from footprint_review_page import FootprintReviewPage

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


class SearchWorker(QObject):
    search_completed = Signal(list)
    search_failed = Signal(str)

    def __init__(self, api_service):
        super().__init__()
        self.api_service = api_service

    def start_search(self, search_term):
        try:
            results = self.api_service.search(search_term)
            self.search_completed.emit(results)
        except Exception as e:
            logger.error(f"SearchWorker failed: {e}", exc_info=True)
            self.search_failed.emit(str(e))


class ImageWorker(QObject):
    image_loaded = Signal(bytes, str)
    image_failed = Signal(str, str)

    def __init__(self, api_service):
        super().__init__()
        self.api_service = api_service

    def load_image(self, image_url: str, image_type: str):
        try:
            image_data = self.api_service.download_image_from_url(image_url)
            if image_data:
                self.image_loaded.emit(image_data, image_type)
            else:
                self.image_failed.emit("Failed to download image", image_type)
        except Exception as e:
            logger.error(f"ImageWorker failed for {image_url}: {e}", exc_info=True)
            self.image_failed.emit(str(e), image_type)


class ComponentWorker(QObject):
    footprint_png_loaded = Signal(bytes)
    footprint_png_failed = Signal(str)

    def __init__(self, api_service):
        super().__init__()
        self.api_service = api_service

    def load_footprint_png(self, lcsc_id: str):
        try:
            png_data = self.api_service.get_footprint_png_with_pads(lcsc_id)
            if png_data:
                self.footprint_png_loaded.emit(png_data)
            else:
                self.footprint_png_failed.emit("Could not generate footprint PNG.")
        except Exception as e:
            logger.error(f"ComponentWorker failed for {lcsc_id}: {e}", exc_info=True)
            self.footprint_png_failed.emit(str(e))


class ClickableLabel(QLabel):
    clicked = Signal()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setCursor(QCursor(Qt.PointingHandCursor))

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)


class WorkbenchController(QObject):
    request_search = Signal(str)
    request_image = Signal(str, str)
    request_footprint_png = Signal(str)

    def __init__(self, window):
        super().__init__()
        self.window = window
        self.api_service = Search()
        self.current_search_result = None
        self.current_step_index = 0

        self._setup_workers()
        self._find_widgets()
        self._connect_signals()
        self.go_to_step(0)

    def _setup_workers(self):
        self.search_thread = QThread()
        self.search_worker = SearchWorker(self.api_service)
        self.search_worker.moveToThread(self.search_thread)
        self.search_worker.search_completed.connect(self.on_search_completed)
        self.search_worker.search_failed.connect(self.on_search_failed)
        self.request_search.connect(self.search_worker.start_search)
        self.search_thread.start()

        self.image_thread = QThread()
        self.image_worker = ImageWorker(self.api_service)
        self.image_worker.moveToThread(self.image_thread)
        self.image_worker.image_loaded.connect(self.on_image_loaded)
        self.image_worker.image_failed.connect(self.on_image_failed)
        self.request_image.connect(self.image_worker.load_image)
        self.image_thread.start()

        self.component_thread = QThread()
        self.component_worker = ComponentWorker(self.api_service)
        self.component_worker.moveToThread(self.component_thread)
        self.component_worker.footprint_png_loaded.connect(self.on_footprint_png_loaded)
        self.component_worker.footprint_png_failed.connect(self.on_footprint_png_failed)
        self.request_footprint_png.connect(self.component_worker.load_footprint_png)
        self.component_thread.start()

    def _find_widgets(self):
        self.main_stack = self.window.findChild(QStackedWidget, "mainStackedWidget")
        self.button_PreviousStep = self.window.findChild(QPushButton, "button_PreviousStep")
        self.button_NextStep = self.window.findChild(QPushButton, "button_NextStep")
        self.context_frame = self.window.findChild(QFrame, "contextFrame")
        self.label_LcscId = self.window.findChild(QLabel, "label_LcscId")
        self.label_PartTitle = self.window.findChild(QLabel, "label_PartTitle")
        self.mfn_value = self.window.findChild(QLabel, "mfn_value")
        self.mfn_part_value = self.window.findChild(QLabel, "mfn_part_value")
        self.description_value = self.window.findChild(QLabel, "description_value")
        self.hero_view = self.window.findChild(QGraphicsView, "image_hero_view")
        
        # --- The magic happens here: find our promoted widget ---
        self.page_Search: SearchPage = self.window.findChild(QWidget, "page_Search")
        self.page_FootprintReview: FootprintReviewPage = self.window.findChild(QWidget, "page_FootprintReview")
        
        self.pages = [
            self.page_Search,
            self.page_FootprintReview,
            self.window.findChild(QWidget, "page_SymbolReview"),
            self.window.findChild(QWidget, "page_ComponentAssembly"),
            self.window.findChild(QWidget, "page_FinalSummary"),
        ]
        
        self.step_labels = []
        self._promote_step_labels()
        
        self.button_ApproveFootprint = self.window.findChild(QPushButton, "button_ApproveFootprint")
        self.button_ApproveSymbol = self.window.findChild(QPushButton, "button_ApproveSymbol")
        self.button_ProceedToFinalize = self.window.findChild(QPushButton, "button_ProceedToFinalize")
        
        self._setup_hero_image()
        self.on_search_item_selected(None)
    
    def _setup_hero_image(self):
        """Initializes the QGraphicsView for the hero image in the sidebar."""
        self.hero_scene = QGraphicsScene()
        self.hero_view.setScene(self.hero_scene)
        self.hero_pixmap_item = QGraphicsPixmapItem()
        self.hero_scene.addItem(self.hero_pixmap_item)


    def _connect_signals(self):
        # --- Connect to the clean, high-level signals from SearchPage ---
        self.page_Search.search_requested.connect(self.run_search)
        self.page_Search.item_selected.connect(self.on_search_item_selected)
        self.page_Search.item_double_clicked.connect(self.next_step)

        if self.button_PreviousStep:
            self.button_PreviousStep.clicked.connect(self.previous_step)
        if self.button_NextStep:
            self.button_NextStep.clicked.connect(self.next_step)
        if self.button_ApproveFootprint:
            self.button_ApproveFootprint.clicked.connect(self.next_step)
        if self.button_ApproveSymbol:
            self.button_ApproveSymbol.clicked.connect(self.next_step)
        if self.button_ProceedToFinalize:
            self.button_ProceedToFinalize.clicked.connect(self.next_step)

    def _promote_step_labels(self):
        """Replace step status labels with clickable versions"""
        step_names = ["step1_Status", "step2_Status", "step3_Status", "step4_Status", "step5_Status"]
        layout = self.context_frame.layout()
        if not layout:
            return
        for i, name in enumerate(step_names):
            old_label = self.window.findChild(QLabel, name)
            if old_label:
                new_label = ClickableLabel(old_label.text())
                new_label.setToolTip(old_label.toolTip())
                new_label.setObjectName(old_label.objectName())
                index = layout.indexOf(old_label)
                layout.insertWidget(index, new_label)
                layout.removeWidget(old_label)
                old_label.deleteLater()
                new_label.clicked.connect(partial(self.go_to_step, i))
                self.step_labels.append(new_label)

    def go_to_step(self, index):
        if not (0 <= index < len(self.pages) and self.pages[index] is not None):
            logger.warning(f"Invalid step index or page not found: {index}")
            return
        self.current_step_index = index
        self.main_stack.setCurrentWidget(self.pages[index])
        for i, label in enumerate(self.step_labels):
            font = label.font()
            font.setBold(i == index)
            label.setFont(font)
        self.button_PreviousStep.setEnabled(index > 0)
        self.button_NextStep.setEnabled(index < len(self.pages) - 1 and self.current_search_result is not None)
        self.window.statusBar().showMessage(f"Step {index + 1}: {self.step_labels[index].text()[3:]}", 2000)

    def next_step(self, item_data=None):
        if self.current_search_result or item_data:
            # If we are on the search page and moving to the next step (footprint review)
            if self.current_step_index == 0:
                # Get the pixmap from the search page's footprint container
                footprint_pixmap = self.page_Search.get_footprint_pixmap()
                # Set the pixmap on the footprint review page
                if self.page_FootprintReview and footprint_pixmap:
                    self.page_FootprintReview.set_footprint_image(footprint_pixmap)

            if self.current_step_index < len(self.pages) - 1:
                self.go_to_step(self.current_step_index + 1)
        else:
            logger.warning("Cannot proceed to next step without a selected component.")
            self.window.statusBar().showMessage("Please select a component first.", 3000)

    def previous_step(self):
        self.go_to_step(self.current_step_index - 1)

    def run_search(self, search_term: str):
        self.page_Search.set_search_button_enabled(False)
        self.page_Search.set_search_button_text("Searching...")
        self.window.statusBar().showMessage(f"Searching for '{search_term}'...")
        self.request_search.emit(search_term)

    def on_search_completed(self, results: List[SearchResult]):
        self.page_Search.update_search_results(results)
        self.window.statusBar().showMessage(f"Found {len(results)} results.", 3000)
        self.page_Search.set_search_button_enabled(True)
        self.page_Search.set_search_button_text("Search")

    def on_search_failed(self, error_message):
        self.window.statusBar().showMessage(f"Search failed: {error_message}", 5000)
        self.page_Search.set_search_button_enabled(True)
        self.page_Search.set_search_button_text("Search")

    def on_image_loaded(self, image_data: bytes, image_type: str):
        pixmap = QPixmap()
        pixmap.loadFromData(image_data)
        if image_type == "hero":
            self.hero_pixmap_item.setPixmap(pixmap)
            if pixmap.isNull() or self.hero_view.width() == 0:
                return  # Don't try to scale if there's no image or view

            # Calculate the scale factor to make the image 1.5x the view's size.
            # We use the larger of the width or height scale factors to ensure the image always covers the view.
            scale_w = (1.5 * self.hero_view.width()) / pixmap.width()
            scale_h = (1.5 * self.hero_view.height()) / pixmap.height()
            scale_factor = max(scale_w, scale_h)

            self.hero_view.resetTransform()
            self.hero_view.scale(scale_factor, scale_factor)
            self.hero_view.centerOn(self.hero_pixmap_item)

        elif image_type == "symbol":
            self.page_Search.set_symbol_image(pixmap)

    def on_image_failed(self, error_message: str, image_type: str):
        logger.warning(f"Image loading failed for {image_type}: {error_message}")
        pixmap = QPixmap()
        if image_type == "hero":
            self.hero_pixmap_item.setPixmap(pixmap)
        elif image_type == "symbol":
            self.page_Search.set_symbol_image(pixmap)

    def on_footprint_png_loaded(self, png_data: bytes):
        pixmap = QPixmap()
        pixmap.loadFromData(png_data)
        self.page_Search.set_footprint_image(pixmap)

    def on_footprint_png_failed(self, error_message: str):
        logger.warning(f"Footprint PNG loading failed: {error_message}")
        self.page_Search.set_footprint_image(QPixmap())

    def on_search_item_selected(self, result: SearchResult):
        """Handles a new item being selected in the SearchPage's tree view."""
        self.current_search_result = result

        # Clear all previous images immediately on selection change
        self.hero_pixmap_item.setPixmap(QPixmap())
        self.page_Search.set_symbol_image(QPixmap())
        self.page_Search.set_footprint_image(QPixmap())

        # Update the main context panel
        if result:
            self.label_LcscId.setText(f"LCSC ID: {result.lcsc_id}")
            self.label_PartTitle.setText(result.part_name)
            self.mfn_value.setText(result.manufacturer)
            self.mfn_part_value.setText(result.mfr_part_number)
            self.description_value.setText(result.description)

            # Request images for the newly selected part
            if result.image_url:
                self.request_image.emit(result.image_url, "hero")
            if result.lcsc_id:
                self.request_footprint_png.emit(result.lcsc_id)
                # TODO: Also request symbol image when available
        else:
            # Clear context panel if no item is selected
            self.label_LcscId.setText("LCSC ID: -")
            self.label_PartTitle.setText("No Part Loaded")
            self.mfn_value.setText("(select a part)")
            self.mfn_part_value.setText("")
            self.description_value.setText("")

        # Enable/disable next button based on selection
        self.button_NextStep.setEnabled(result is not None)

    def cleanup(self):
        for thread in [self.search_thread, self.image_thread, self.component_thread]:
            if thread.isRunning():
                thread.quit()
                thread.wait()

def main():
    app = QApplication(sys.argv)
    
    # --- IMPORTANT ---
    # Register the custom widget with the loader so it knows how to handle it.
    loader = QUiLoader()
    loader.registerCustomWidget(SearchPage)
    loader.registerCustomWidget(FootprintReviewPage)
    
    ui_file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "workbench.ui")
    window = loader.load(ui_file_path, None)
    
    if not window:
        logger.critical(f"Error loading workbench.ui: {loader.errorString()}")
        sys.exit(1)
        
    controller = WorkbenchController(window)
    window.show()
    app.aboutToQuit.connect(controller.cleanup)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
