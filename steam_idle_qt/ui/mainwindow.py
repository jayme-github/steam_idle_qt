# -*- coding: utf-8 -*-

"""
Module implementing MainWindow.
"""
import os
import logging
from PyQt4.QtCore import pyqtSlot, Qt, QThread, pyqtSignal, QMetaObject, Q_ARG, QTimer, QSettings, QPoint, QSize
from PyQt4.QtGui import QMainWindow, QTableWidgetItem, QProgressBar, QPixmap, QIcon, QHeaderView, QLabel, QDialog

from .Ui_mainwindow import Ui_MainWindow, _fromUtf8, _translate
from .settingsdialog import SettingsDialog
from steam_idle_qt.QSteamWebBrowser import QSteamWebBrowser
from steam_idle_qt.QIdle import Idle, MultiIdle
from steam_idle.page_parser import SteamBadges, App

class ParseApps(QThread):
    steamDataReady = pyqtSignal(dict)
    def __init__(self, sbb):
        super(ParseApps, self).__init__()
        self.sbb = sbb
    def run(self):
        logger = logging.getLogger('.'.join((__name__, self.__class__.__name__)))
        logger.info('Updating apps from steam')
        apps = self.sbb.get_apps()
        logger.debug('ParseApps: {}'.format(apps))
        self.steamDataReady.emit(apps)

class MainWindow(QMainWindow, Ui_MainWindow):
    """
    Class documentation goes here.
    """
    apps = None
    activeApps = [] # List of apps currently ideling
    continueToNext = True
    _idleThread = None
    _multiIdleThread = None
    _threadParseApps = None
    _steamPassword = None

    def __init__(self, parent=None):
        """
        Constructor

        @param parent reference to the parent widget (QWidget)
        """
        super().__init__(parent)
        self.logger = logging.getLogger('.'.join((__name__, self.__class__.__name__)))
        self.logger.debug('Setting up UI')
        self.setupUi(self)
        self.logger.debug('Setting up UI DONE')
        self.labelTotalGamesToIdle.hide()
        self.labelTotalGamesInRefund.hide()
        self.labelTotalRemainingDrops.hide()
        self.statusBar.setMaximumHeight(20)
        self.progressBar = QProgressBar(self)
        self.progressBar.setRange(0,1)
        self.progressBar.setFormat('')
        self.progressBar.setMaximumHeight(self.statusBar.height())
        self.progressBar.setMaximumWidth(self.statusBar.width())
        self.labelStatusBar = QLabel()
        self.statusBar.addPermanentWidget(self.labelStatusBar)
        self.statusBar.addPermanentWidget(self.progressBar)

        # No resize and no sorting for status column
        self.tableWidgetGames.horizontalHeader().setResizeMode(0, QHeaderView.ResizeToContents)
        self.tableWidgetGames.selectionModel().currentRowChanged.connect(self.on_tableWidgetGamesSelectionModel_currentRowChanged)

        # Restore settings
        self.readSettings()

        if not os.path.exists(self.settings.fileName()) or self.settings.value('steam/password', None) == None:
            # Init Settings and/or ask for password
            self.showSettings()
        else:
            self.logger.debug('Init done')
            self.startProgressBar('Loading data from Steam...')
            # All slower init work is launched via singleShot timer so the UI is displayed immediately
            QTimer.singleShot(50, self.slowInit) # 100 msec seems delays slowInit for too long
            self.logger.debug('initDone signal sent')

    @property
    def settings(self):
        return QSettings(QSettings.IniFormat, QSettings.UserScope, 'jayme-github', 'SteamIdle')

    @property
    def steamPassword(self):
        settings = self.settings
        password = settings.value('steam/password', '')
        if password != '':
            return password

        if self._steamPassword != None:
            return self._steamPassword

        raise Exception('No password')

    @pyqtSlot()
    def showSettings(self):
        settingsDialog = SettingsDialog(parent=self)
        if settingsDialog.exec_() == QDialog.Accepted:
            self.logger.info('SettingsDialog accepted')
            self.slowInit()
        self.logger.info('SettingsDialog NOT accepted')

    def readSettings(self):
        settings = self.settings
        self.logger.debug('Reading settings from "%s"', settings.fileName())
        pos = settings.value('pos', QPoint(200, 200))
        size = settings.value('size', QSize(650, 500))
        self.resize(size)
        self.move(pos)

    def writeSettings(self):
        settings = self.settings
        settings.setValue("pos", self.pos())
        settings.setValue("size", self.size())

    @pyqtSlot()
    def slowInit(self):
        ''' All init stuff that takes time should be done in here to not
            delay the UI startup (fast as possible response to the user).
        '''
        # Setup SteamWebBrowser etc.
        self.logger.debug('Init QSteamWebBrowser')
        swb = QSteamWebBrowser(
                username=self.settings.value('steam/username'),
                password=self.steamPassword,
                parent=self
        )

        data_path = os.path.join(os.path.dirname(self.settings.fileName()), 'SteamIdle')
        self.logger.debug('Using data path: "%s"', data_path)
        self.sbb = SteamBadges(swb, data_path)

        # Create a thread for parsing apps and connect signal
        self._threadParseApps = ParseApps(self.sbb)
        self._threadParseApps.steamDataReady.connect(self.updateSteamData)

        # Create worker and thread for ideling
        self._idleThread = QThread()
        self._idleInstance = Idle(self.sbb)
        self._idleInstance.moveToThread(self._idleThread)
        self._idleInstance.appDone.connect(self.on_idleAppDone)           # called when app has finished ideling
        self._idleInstance.statusUpdate.connect(self.on_idleStatusUpdate) # called on new idle period (e.g. new delay)
        self._idleInstance.steamDataReady.connect(self.updateSteamData)   # called then idle thread has updated steam badge data
        self._idleInstance.finished.connect(self._post_stopIdle)          # called on thread exit, update UI etc.
        self._idleInstance.finished.connect(self._idleThread.quit)

        # Create worker and thread for multi idle
        self._multiIdleThread = QThread()
        self._multiIdleInstance = MultiIdle()
        self._multiIdleInstance.moveToThread(self._multiIdleThread)
        self._multiIdleInstance.statusUpdate.connect(self.on_idleStatusUpdate) # called on start and when idle childs finish
        self._multiIdleInstance.allDone.connect(self.on_multiIdleFinished)     # called when all apps are done
        self._multiIdleInstance.appDone.connect(self.on_multiIdleAppDone)      # called when one app is done
        self._multiIdleInstance.finished.connect(self._post_stopIdle)          # called on thread exit, update UI etc.
        self._multiIdleInstance.finished.connect(self._multiIdleThread.quit)

        # Update the tableWidgetGames (e.g. start _threadParseApps)
        self.on_actionRefresh_triggered()

    @pyqtSlot(str)
    def on_idleStatusUpdate(self, msg):
        #TODO Would be nice to have some timer ticking in statusbar
        self.logger.debug('Got idleStatusUpdate: %s' %msg)
        self.startProgressBar(msg)

    @pyqtSlot(str)
    def startProgressBar(self, message):
        self.logger.debug('startProgressBar: %s' %message)
        self.labelStatusBar.setText(message)
        self.progressBar.setToolTip(message)
        self.progressBar.show()
        self.progressBar.setRange(0,0)

    def stopProgressBar(self):
        self.labelStatusBar.clear()
        self.progressBar.setRange(0,1)
        self.progressBar.setToolTip('')
        self.progressBar.hide()

    def startIdle(self, app):
        self._idleThread.start()
        self.activeApps = [app]
        QMetaObject.invokeMethod(self._idleInstance, 'doStartIdle', Qt.QueuedConnection,
                                    Q_ARG(App, app))
        # Enable nextAction (if more than one app to idle)
        if self.totalGamesToIdle > 1 and self.continueToNext:
            self.actionNext.setEnabled(True)
        self._post_startIdle()

    def startMultiIdle(self):
        self._multiIdleThread.start()
        self.activeApps = [a for a in self.apps.values() if a.playTime < 2.0 and a.remainingDrops > 0]
        self.logger.debug('startMultiIdle for {} apps: {}'.format(len(self.activeApps), self.activeApps))
        QMetaObject.invokeMethod(self._multiIdleInstance, 'doStartIdle', Qt.QueuedConnection,
                                    Q_ARG(list, self.activeApps))
        self.actionNext.setEnabled(False)
        self._post_startIdle()

    def _post_startIdle(self):
        ''' Update UI stuff (icons, table etc.) after starting idle '''
        self.actionMultiIdle.setEnabled(False) # One can't change multi-idle during idle
        # Switch to stop icon/text
        self.actionStartStop.setText(_translate("MainWindow", '&Stop', None))
        self.actionStartStop.setToolTip(_translate("MainWindow", 'Stop ideling', None))
        self.actionStartStop.setIcon(QIcon.fromTheme(_fromUtf8('media-playback-stop')))
        # Update statusCell(s)
        for app in self.activeApps:
            self.tableWidgetGames.item(
                self.rowIdForAppId(app.appid), 0
            ).setIcon(QIcon.fromTheme(_fromUtf8('media-playback-start')))

    def stopIdle(self):
        QMetaObject.invokeMethod(self._idleInstance, 'doStopIdle', Qt.QueuedConnection)

    def stopMultiIdle(self):
        QMetaObject.invokeMethod(self._multiIdleInstance, 'doStopIdle', Qt.QueuedConnection)

    @pyqtSlot()
    def _post_stopIdle(self):
        ''' Update UI stuff (icons, table etc.) after stopping idle '''
        # Update statusCells
        for app in self.activeApps:
            self.tableWidgetGames.item(
                self.rowIdForAppId(app.appid), 0
            ).setIcon(QIcon())
        # remove active apps and stop progressbar
        self.activeApps = []
        self.stopProgressBar()
        # Disable nextAction
        self.actionNext.setEnabled(False)
        # Switch to start icon/text
        self.actionStartStop.setText(_translate("MainWindow", '&Start', None))
        self.actionStartStop.setToolTip(_translate("MainWindow", 'Start ideling', None))
        self.actionStartStop.setIcon(QIcon.fromTheme(_fromUtf8('media-playback-start')))
        self.on_actionRefresh_triggered() #Update data

    def rowIdForAppId(self, appid):
        ''' Returns the rowId that contains appid or -1 if it was not found
        '''
        matches = self.tableWidgetGames.model().match(
            self.tableWidgetGames.model().index(0,0),
            Qt.UserRole,
            appid,
            hits=1,
        )
        return matches[0].row() if matches else -1

    def nextAppWithDrops(self, startAt=0):
        ''' Return the next app with remaining drops or None'''
        for rowId in range(startAt, self.tableWidgetGames.rowCount()):
            app = self.tableWidgetGames.item(rowId, 1).data(Qt.UserRole)
            if app.remainingDrops > 0:
                return app
        return None

    def add_updateRow(self, app):
        ''' Updates entries in the tableView with new data, adds new rows if needed
        '''
        rowId = self.rowIdForAppId(app.appid)
        if rowId >= 0:
            # Update existing row
            # If this app is ideling atm, add a icon
            if app in self.activeApps:
                self.tableWidgetGames.item(rowId, 0).setIcon(QIcon.fromTheme(_fromUtf8('media-playback-start')))
            # Remaining drops
            self.tableWidgetGames.item(rowId, 2).setData(Qt.EditRole, app.remainingDrops)
            # Playtime
            self.tableWidgetGames.item(rowId, 3).setData(Qt.EditRole, app.playTime)
        else:
            # Add a game row to the table
            rowId = self.tableWidgetGames.rowCount()
            self.tableWidgetGames.insertRow(rowId)

            # Cells are: State, Game, Remaining drops, Playtime
            stateCell = QTableWidgetItem()
            # If this app is ideling atm, add a icon
            if app in self.activeApps:
                stateCell.setIcon(QIcon.fromTheme(_fromUtf8('media-playback-start')))
            # Use appid as identifier to look up apps in table
            stateCell.setData(Qt.UserRole, app.appid)

            gameCell = QTableWidgetItem(app.name)
            # Store app instance (can't be looked up via model.match() for some reason)
            gameCell.setData(Qt.UserRole, app)
            if os.path.exists(app.icon):
                # Load pixmap and create an icon
                gameIcon = QIcon(QPixmap(app.icon))
                gameCell.setIcon(gameIcon)

            remainingDropsCell = QTableWidgetItem()
            remainingDropsCell.setData(Qt.EditRole, app.remainingDrops) # Use setData to have numeric instead of alpha-numeric sorting

            playtimeCell = QTableWidgetItem()
            playtimeCell.setData(Qt.EditRole, app.playTime)

            # Add cells
            self.tableWidgetGames.setItem(rowId, 0, stateCell)
            self.tableWidgetGames.setItem(rowId, 1, gameCell)
            self.tableWidgetGames.setItem(rowId, 2, remainingDropsCell)
            self.tableWidgetGames.setItem(rowId, 3, playtimeCell)

        # Hide row if there no drops remain and actionShowAll is not checked
        if self.actionShowAll.isChecked() or app.remainingDrops > 0:
            self.tableWidgetGames.setRowHidden(rowId, False)
        else:
            self.tableWidgetGames.setRowHidden(rowId, True)

    @pyqtSlot(dict)
    def updateSteamData(self, apps=None):
        ''' Update UI with data from steam
            will use the apps provided as parameter or self.apps
        '''
        if apps != None:
            self.apps = apps

        if self.apps != None:
            #TODO: get selected row and reselect after pouplation
            self.totalGamesToIdle = 0
            self.totalRemainingDrops = 0
            self.gamesInRefundPeriod = 0

            # Temporarily disable sorting, see http://doc.qt.io/qt-5/qtablewidget.html#setItem
            self.tableWidgetGames.setSortingEnabled(False)
            try:
                self.tableWidgetGames.horizontalHeader().sortIndicatorChanged.disconnect(self.tableWidgetGames.resizeRowsToContents)
            except TypeError:
                # Raises TypeError if not connected:
                # TypeError: disconnect() failed between 'sortIndicatorChanged' and 'resizeRowsToContents'
                pass

            for _, app in self.apps.items():
                self.totalRemainingDrops += app.remainingDrops
                if app.remainingDrops > 0:
                    self.totalGamesToIdle += 1
                    if app.playTime < 2.0:
                        self.gamesInRefundPeriod += 1
                self.add_updateRow(app)

            # Re-Enable sorting
            self.tableWidgetGames.setSortingEnabled(True)
            self.tableWidgetGames.horizontalHeader().sortIndicatorChanged.connect(self.tableWidgetGames.resizeRowsToContents)

            # Update cell and row sizes
            self.tableWidgetGames.resizeColumnsToContents()
            self.tableWidgetGames.resizeRowsToContents()

            # Update labels
            self.labelTotalGamesToIdle.setText(self.tr('{} games left to idle').format(self.totalGamesToIdle))
            self.labelTotalGamesToIdle.show()
            self.labelTotalGamesInRefund.setText(self.tr('{} games in refund period (<2h play time)').format(self.gamesInRefundPeriod))
            self.labelTotalGamesInRefund.show()
            self.labelTotalRemainingDrops.setText(self.tr('{} remaining card drops').format(self.totalRemainingDrops))
            self.labelTotalRemainingDrops.show()

            # Leave actions untuched if idle is running
            if not self.activeApps:
                # Enable actionStartStop if there are apps to idle
                if len(self.apps) > 0:
                    self.actionStartStop.setEnabled(True)

                # Enable/Disable actionMultiIdle
                if self.gamesInRefundPeriod >= 2:
                    # Enable and check by default
                    # TODO: Configuration item for multi-idle as default
                    self.actionMultiIdle.setChecked(True)
                    self.actionMultiIdle.setEnabled(True)
                else:
                    # Not enough apps for multi-idle, uncheck and disable
                    self.actionMultiIdle.setChecked(False)
                    self.actionMultiIdle.setEnabled(False)

        # Done, stop progressBar if it was updates for this refresh
        if self.labelStatusBar.text() == 'Loading data from Steam...':
            self.stopProgressBar()

    def cleanUp(self):
        if len(self.activeApps) == 1:
            self.logger.debug('cleanUp: stopIdle()')
            # Something is running, stop
            self.stopIdle()
        elif len(self.activeApps) > 1:
            self.logger.debug('cleanUp: stopMultiIdle()')
            # Stop Multi-Idleif enabled
            self.stopMultiIdle()
        self.logger.debug('cleanUp: stopProgressBar')
        self.stopProgressBar()
        if self._idleThread:
            self.logger.debug('cleanUp: idleThread.quit()')
            self._idleThread.quit()
            self.logger.debug('cleanUp: _idleThread.wait()')
            self._idleThread.wait()
        if self._multiIdleThread:
            self.logger.debug('cleanUp: _multiIdleThread.quit()')
            self._multiIdleThread.quit()
            self.logger.debug('cleanUp: _multiIdleThread.wait()')
            self._multiIdleThread.wait()
        self.logger.debug('cleanUp: DONE')

    def closeEvent(self, event):
        self.writeSettings()
        self.cleanUp()
        event.accept()

    @pyqtSlot()
    def on_actionQuit_triggered(self):
        self.close()

    @pyqtSlot()
    def on_multiIdleFinished(self):
        self.activeApps = []
        self.updateSteamData() # This will disable actionMultiIdle etc.
        self.on_actionStartStop_triggered() # Start normal idle process

    @pyqtSlot()
    def on_actionStartStop_triggered(self):
        self.continueToNext = True
        if len(self.activeApps) == 1:
            # Something is running, stop
            self.stopIdle()
        elif len(self.activeApps) > 1:
            # Stop Multi-Idleif enabled
            self.logger.debug('stop multiidle')
            self.stopMultiIdle()
        else:
            # Nothing is running
            if self.actionMultiIdle.isChecked():
                # Start Multi-Idleif enabled
                self.logger.debug('start multiidle')
                self.startMultiIdle()
            else:
                # Start with the first app in table
                self.startIdle(self.nextAppWithDrops())

    @pyqtSlot()
    def on_actionRefresh_triggered(self):
        if not self.labelStatusBar.text():
            self.startProgressBar('Loading data from Steam...')
        self._threadParseApps.start()

    @pyqtSlot('QModelIndex', 'QModelIndex')
    def on_tableWidgetGamesSelectionModel_currentRowChanged(self, current, previous):
        gameCell = self.tableWidgetGames.item(current.row(), 1) # Get the gameCell of this row
        app = gameCell.data(Qt.UserRole)
        if os.path.exists(app.header):
            headerPixmap = QPixmap(app.header)
        else:
            headerPixmap = QPixmap('NoImage.png')
        self.labelHeaderImage.setPixmap(headerPixmap)

    @pyqtSlot(bool)
    def on_actionShowAll_triggered(self, checked):
        if checked:
            # Re-populate table with all apps
            self.updateSteamData()
        else:
            # Hide all rows with apps that have no drops remaining
            matches = self.tableWidgetGames.model().match(
                self.tableWidgetGames.model().index(0,2),
                Qt.EditRole,
                0,
                hits=-1,
            )
            for m in matches:
                self.tableWidgetGames.setRowHidden(m.row(), True)

    @pyqtSlot()
    def on_actionNext_triggered(self):
        ''' If next action is triggered, update data from steam and idle the next app '''
        self.logger.debug('on_actionNext_triggered')
        self.on_actionRefresh_triggered()
        self.on_idleAppDone()

    @pyqtSlot(App)
    def on_idleAppDone(self, app=None):
        if not self.continueToNext:
            self.stopIdle()

        rowId = self.rowIdForAppId(self.activeApps[0].appid) # Assume there is only one active app as actionNext is disabled in MultiIdle
        nextApp = self.nextAppWithDrops(startAt=rowId+1)
        if nextApp:
            # Update icon of old statusCell
            self.tableWidgetGames.item(rowId, 0).setIcon(QIcon())

            # Load the next app into idle thread
            self.startIdle(nextApp)
            if rowId + 1 == self.tableWidgetGames.rowCount() - 1:
                # This was the last app(/row), disable next button
                self.actionNext.setEnabled(False)
        else:
            # No row with this id: stop
            self.stopIdle()
            self.updateSteamData() # This will update the table and enable/disable buttons as needed

    @pyqtSlot(App)
    def on_multiIdleAppDone(self, app):
        rowId = self.rowIdForAppId(app.appid)
        self.logger.debug('on_multiIdleAppDone, removing icon from row: %d' %rowId)
        self.tableWidgetGames.item(rowId, 0).setIcon(QIcon()) # Remove "running" icon from app
        self.on_actionRefresh_triggered()

    @pyqtSlot(int, int)
    def on_tableWidgetGames_cellDoubleClicked(self, row, column):
        if len(self.activeApps) == 1:
            # Stop currently ideling game
            self.stopIdle()
        elif len(self.activeApps) > 1:
            # Stop MultiIdle
            self.stopMultiIdle()
        self.actionMultiIdle.setEnabled(False)
        self.continueToNext = False # Don't continue to next app
        app = self.tableWidgetGames.item(row, 1).data(Qt.UserRole)
        self.startIdle(app)

    @pyqtSlot()
    def on_actionSettings_triggered(self):
        self.showSettings()
