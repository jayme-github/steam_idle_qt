# -*- coding: utf-8 -*-

"""
Module implementing MainWindow.
"""
import os
import logging
from itertools import chain
from datetime import timedelta
from PyQt4.QtCore import pyqtSlot, Qt, QThread, QDir, pyqtSignal, QMetaObject, Q_ARG, QTimer, QSettings, QPoint, QSize, QUrl
from PyQt4.QtGui import QMainWindow, QTableWidgetItem, QProgressBar, QPixmap, QIcon, QHeaderView, QLabel, QDialog, QMenu, QDesktopServices

from .Ui_mainwindow import Ui_MainWindow, _fromUtf8, _translate
from .settingsdialog import SettingsDialog
from steam_idle_qt.QIdle import Idle, MultiIdle
from steam_idle.page_parser import App
from steam_idle_qt.QSteamParser import QSteamParser
from steam_idle import steam_api

class MainWindow(QMainWindow, Ui_MainWindow):
    """
    Class documentation goes here.
    """
    apps = {}
    activeApps = [] # List of app instances currently ideling
    totalGamesToIdle = 0
    gamesInRefundPeriod = 0
    totalRemainingDrops = 0
    _idleThread = None
    _multiIdleThread = None
    _SteamParserThread = None
    _steamPassword = None
    _checkSteamRunningTimer = None
    _init_done = False # True if initialization is completed (loaded data from steam etc.)
    _startup = True # True on app start, set to false then init is done (and steam is running).
    _statusBarTimer = None
    _statusBarTimerDelta = None
    steamDataUpdated = pyqtSignal() # Emitted when tableView has been populated with fresh steam data

    def __init__(self, parent=None):
        """
        Constructor

        @param parent reference to the parent widget (QWidget)
        """
        super(MainWindow, self).__init__(parent)
        self.logger = logging.getLogger('.'.join((__name__, self.__class__.__name__)))
        self.logger.debug('Setting up UI')
        self.setupUi(self)
        self.logger.debug('Setting up UI DONE')
        self.labelTotalGamesToIdle.hide()
        self.labelTotalGamesInRefund.hide()
        self.labelTotalRemainingDrops.hide()
        self.labelSteamNotRunning.hide()
        self.statusBar.setMaximumHeight(20)
        self.progressBar = QProgressBar(self)
        self.progressBar.setRange(0,1)
        self.progressBar.setFormat('')
        self.progressBar.setMaximumHeight(self.statusBar.height())
        self.progressBar.setMaximumWidth(self.statusBar.width())
        self.labelStatusBar = QLabel()
        self.labelStatusBarTimer = QLabel()
        self.statusBar.addWidget(self.labelStatusBarTimer)
        self.statusBar.addPermanentWidget(self.labelStatusBar)
        self.statusBar.addPermanentWidget(self.progressBar)

        # No resize and no sorting for status column
        self.tableWidgetGames.horizontalHeader().setResizeMode(0, QHeaderView.ResizeToContents)
        self.tableWidgetGames.selectionModel().currentRowChanged.connect(self.on_tableWidgetGamesSelectionModel_currentRowChanged)

        # Restore settings
        self.readSettings()

        self.checkSteamRunning()

        if not os.path.exists(QDir.toNativeSeparators(self.settings.fileName())) or self.settings.value('steam/password', None) == None:
            # Init Settings and/or ask for password
            self.showSettings()
        else:
            self.logger.debug('Init done')
            self.startProgressBar('Loading data from Steam...')
            # All slower init work is launched via singleShot timer so the UI is displayed immediately
            QTimer.singleShot(50, self.slowInit) # 100 msec seems delays slowInit for too long
            self.logger.debug('initDone signal sent')

    @pyqtSlot()
    def slowInit(self):
        ''' All init stuff that takes time should be done in here to not
            delay the UI startup (fast as possible response to the user).
        '''
        data_path = os.path.join(
            os.path.dirname(QDir.toNativeSeparators(self.settings.fileName())),
            'SteamIdle'
        )

        self._SteamParserThread = QThread()
        self._SteamParserInstance = QSteamParser(
            username=self.settings.value('steam/username'),
            password=self.steamPassword,
            data_path=data_path
        )
        self._SteamParserInstance.moveToThread(self._SteamParserThread)
        self._SteamParserInstance.steamDataReady.connect(self.updateSteamData)
        self._SteamParserInstance.timerStart.connect(self.on_SteamParser_startTimer)
        self._SteamParserInstance.timerStop.connect(self.on_SteamParser_stopTimer)
        # Restart the statusbar timer with every timeout
        self._SteamParserInstance.timerTimeout.connect(self.on_SteamParser_startTimer)
        self._SteamParserThread.start()

        # Create worker and thread for ideling
        self._idleThread = QThread()
        self._idleInstance = Idle()
        self._idleInstance.moveToThread(self._idleThread)
        # Connect signals
        # called when app has finished ideling
        self._idleInstance.appDone.connect(self.on_idleAppDone)
        # called on new idle period (e.g. new delay)
        self._idleInstance.statusUpdate.connect(self.on_idleStatusUpdate)
        # Update steam data (apps) in idleInstance (called periodically by QStremParser)
        self._SteamParserInstance.steamDataReady.connect(self._idleInstance.on_steamDataReady)
        # Update/Start SteamParserTimer with new interval
        self._idleInstance.updateSteamParserTimer.connect(self._SteamParserInstance.startTimer)
        # called on thread exit, update UI, stop SteamParser timer
        self._idleInstance.finished.connect(self._post_stopIdle)
        self._idleInstance.finished.connect(self._idleThread.quit)
        self._idleInstance.finished.connect(self._SteamParserInstance.stopTimer)

        # Create worker and thread for multi idle
        self._multiIdleThread = QThread()
        self._multiIdleInstance = MultiIdle()
        self._multiIdleInstance.moveToThread(self._multiIdleThread)
        # Connect signals
        # called on start and when idle childs finish
        self._multiIdleInstance.statusUpdate.connect(self.on_idleStatusUpdate)
        # called when one app is done
        self._multiIdleInstance.appDone.connect(self.on_multiIdleAppDone)
        # called when all apps are done
        self._multiIdleInstance.allDone.connect(self.on_multiIdleFinished)
        # Update steam data (apps) in multiIdleInstance (called periodically by QStremParser)
        self._SteamParserInstance.steamDataReady.connect(self._multiIdleInstance.on_steamDataReady)
        # Update/Start SteamParserTimer with new interval
        self._multiIdleInstance.updateSteamParserTimer.connect(self._SteamParserInstance.startTimer)
        # called on thread exit, update UI etc.
        self._multiIdleInstance.finished.connect(self._post_stopIdle)
        self._multiIdleInstance.finished.connect(self._multiIdleThread.quit)
        self._multiIdleInstance.finished.connect(self._SteamParserInstance.stopTimer)

        # Update the tableWidgetGames
        self.on_actionRefresh_triggered()

        self._init_done = True

    def checkSteamRunning(self):
        if steam_api.IsSteamRunning():
            if self.labelSteamNotRunning.isVisible():
                self.logger.debug('Steam client is running')
            self.labelSteamNotRunning.hide()
            # Skipp that stuff if idle is running
            if len(self.activeApps) < 1:
                self.toggle_actionStartStopIdle()
                self.toggle_actionStartStopMultiIdle()

                # Autostart
                if self._init_done and self._startup:
                    self._startup = False
                    autostartMode = self.settings.value('autostart', 'None')
                    self.logger.info('autostartMode: "%s"', autostartMode)
                    if autostartMode == 'Multi-Idle':
                        MIThreshold = self.settings.value('multiidlethreshold', 2, type=int)
                        if self.gamesInRefundPeriod < MIThreshold:
                            # Number of games in refund is below threshold, start normal idle
                            self.logger.debug('Number of games in refund is below threshold, start normal idle')
                            autostartMode = 'Idle'
                        else:
                            self.logger.debug('Autostart MultiIdle')
                            self.on_actionStartStopMultiIdle_triggered()

                    if autostartMode == 'Idle':
                        self.logger.debug('Autostart Idle')
                        self.on_actionStartStopIdle_triggered()

        else:
            if not self.labelSteamNotRunning.isVisible():
                self.logger.warning('Steam client is not running')
            # Stop Idle processes
            self.labelSteamNotRunning.show()
            self.cleanUp()
            self.actionStartStopIdle.setEnabled(False)
            self.actionNext.setEnabled(False)
            self.actionStartStopMultiIdle.setEnabled(False)

        if not self._checkSteamRunningTimer:
            self.logger.debug('Setting up timer')
            self._checkSteamRunningTimer = QTimer()
            self._checkSteamRunningTimer.timeout.connect(self.checkSteamRunning)
            self._checkSteamRunningTimer.start(15*1000)

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
        self.logger.debug('Reading settings from "%s"',
            QDir.toNativeSeparators(settings.fileName())
        )
        pos = settings.value('pos', QPoint(200, 200))
        size = settings.value('size', QSize(650, 500))
        self.resize(size)
        self.move(pos)

    def writeSettings(self):
        settings = self.settings
        settings.setValue("pos", self.pos())
        settings.setValue("size", self.size())

    @pyqtSlot(str)
    def on_idleStatusUpdate(self, msg):
        #TODO Would be nice to have some timer ticking in statusbar
        self.logger.debug('Got idleStatusUpdate: %s', msg)
        self.startProgressBar(msg)

    @pyqtSlot(str)
    def startProgressBar(self, message):
        self.logger.debug('startProgressBar: %s', message)
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
        # make sure last idle child has stopped
        if self._idleThread:
            self._idleThread.quit()
            self._idleThread.wait()
        self._idleThread.start()
        self.activeApps = [app]
        self.logger.debug('activeApps: "%s"', self.activeApps)
        QMetaObject.invokeMethod(self._idleInstance, 'doStartIdle', Qt.QueuedConnection,
                                    Q_ARG(App, app))
        # Enable nextAction (if more than one app to idle)
        if self.totalGamesToIdle > 1:
            self.actionNext.setEnabled(True)
        self.actionStartStopMultiIdle.setEnabled(False)
        self._post_startIdle()

    def startMultiIdle(self):
        # make sure last idle child has stopped
        if self._multiIdleThread:
            self._multiIdleThread.quit()
            self._multiIdleThread.wait()
        self._multiIdleThread.start()
        self.activeApps = [a for a in self.apps.values() if a.playTime < 2.0 and a.remainingDrops > 0]
        self.logger.debug('startMultiIdle for %d apps: %s', len(self.activeApps), self.activeApps)
        QMetaObject.invokeMethod(self._multiIdleInstance, 'doStartIdle', Qt.QueuedConnection,
                                    Q_ARG(list, self.activeApps))
        self.actionNext.setEnabled(False)
        self.actionStartStopIdle.setEnabled(False)
        self._post_startIdle()

    def _post_startIdle(self):
        ''' Update UI stuff (icons, table etc.) after starting idle '''
        self.logger.debug('activeApps: "%s"', self.activeApps)
        # Switch to stop icon/text
        if len(self.activeApps) > 1:
            # MultiIdle
            self.actionStartStopMultiIdle.setText(_translate("MainWindow", 'Stop &MultiIdle', None))
            self.actionStartStopMultiIdle.setToolTip(_translate("MainWindow", 'Stop MultiIdle', None))
            self.actionStartStopMultiIdle.setIcon(QIcon.fromTheme(_fromUtf8('media-playback-stop')))
        else:
            # Idle
            self.actionStartStopIdle.setText(_translate("MainWindow", '&Stop Idle', None))
            self.actionStartStopIdle.setToolTip(_translate("MainWindow", 'Stop ideling', None))
            self.actionStartStopIdle.setIcon(QIcon.fromTheme(_fromUtf8('media-playback-stop')))

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
        self.logger.debug('activeApps: "%s"', self.activeApps)
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
        self.actionStartStopMultiIdle.setText(_translate("MainWindow", 'Start &MultiIdle', None))
        self.actionStartStopMultiIdle.setToolTip(_translate("MainWindow", 'Start parallel idle of all games in refund period', None))
        self.actionStartStopMultiIdle.setIcon(QIcon.fromTheme(_fromUtf8('media-playback-start')))

        self.actionStartStopIdle.setText(_translate("MainWindow", '&Start Idle', None))
        self.actionStartStopIdle.setToolTip(_translate("MainWindow", 'Start ideling', None))
        self.actionStartStopIdle.setIcon(QIcon.fromTheme(_fromUtf8('media-playback-start')))

        # Update data
        self.on_actionRefresh_triggered()

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
        ''' Return the next app with remaining drops or None
            Will go from at index startAt to startAt -1 (e.g. starts from the begining is end is reached)
        '''
        for rowId in chain(range(startAt, self.tableWidgetGames.rowCount()), range(0, startAt)):
            app = self.appInRow(rowId)
            self.logger.debug('(%d, %d): %s', rowId, self.tableWidgetGames.visualRow(rowId), str(app))
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
            # Update app instance in UserRole
            self.tableWidgetGames.item(rowId, 1).setData(Qt.UserRole, app)
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
        self.logger.debug('updateSteamData with %d apps as parameter',
            len(apps) if apps is not None else 0
        )

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
            if len(self.activeApps) < 1:
                self.toggle_actionStartStopIdle()
                self.toggle_actionStartStopMultiIdle()

        # Done, stop progressBar if it was updates for this refresh
        if self.labelStatusBar.text() == 'Loading data from Steam...':
            self.stopProgressBar()

        self.steamDataUpdated.emit()

    def toggle_actionStartStopIdle(self):
        # Enable actionStartStopIdle if there are apps to idle
        if len(self.apps) > 0:
            self.actionStartStopIdle.setEnabled(True)
        else:
            self.actionStartStopIdle.setEnabled(False)

    def toggle_actionStartStopMultiIdle(self):
        # Enable/Disable actionStartStopMultiIdle
        if self.gamesInRefundPeriod >= 2:
            self.actionStartStopMultiIdle.setEnabled(True)
        else:
            # Not enough apps for multi-idle, disable
            self.actionStartStopMultiIdle.setEnabled(False)

    def cleanUp(self):
        if len(self.activeApps) == 0:
            self.logger.debug('No cleanup needed')
            return

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

    def appInRow(self, rowId):
        return self.tableWidgetGames.item(rowId, 1).data(Qt.UserRole)

    @pyqtSlot()
    def on_actionQuit_triggered(self):
        self.close()

    @pyqtSlot()
    def on_multiIdleFinished(self):
        ''' Will be called when multiIdle has finished all games
            Connect to the steamDataUpdated signal which will be emitted by:
            _multiIdleInstance.finished
             -> _post_stopIdle
              -> on_actionRefresh_triggered
               -> .steamDataReady
        '''
        self.logger.debug('on_multiIdleFinished')
        self.logger.debug('activeApps: "%s"', self.activeApps)
        if len(self.activeApps) > 0:
            raise AssertionError('activeApps should be empty!')

        def _updateDone():
            try:
                self.steamDataUpdated.disconnect(_updateDone)
            except (TypeError, AttributeError):
                pass
            self.logger.debug('Calling startIdle')
            self.startIdle(self.nextAppWithDrops())
        # start idle for first app with drops as soon as steam data is updated
        self.steamDataUpdated.connect(_updateDone)

    @pyqtSlot()
    def on_actionStartStopIdle_triggered(self):
        if len(self.activeApps) == 1:
            # Something is running, stop
            self.logger.debug('stop idle')
            self.stopIdle()
        elif len(self.activeApps) > 1:
            # Stop Multi-Idleif enabled
            self.logger.debug('stop multiidle')
            self.stopMultiIdle()
        else:
            # Nothing is running
            # Start with the first app in table
            self.startIdle(self.nextAppWithDrops())

    @pyqtSlot()
    def on_actionRefresh_triggered(self):
        if not self.labelStatusBar.text():
            self.startProgressBar('Loading data from Steam...')
        QMetaObject.invokeMethod(self._SteamParserInstance, 'updateApps',
                                 Qt.QueuedConnection)

    @pyqtSlot('QModelIndex', 'QModelIndex')
    def on_tableWidgetGamesSelectionModel_currentRowChanged(self, current, previous):
        app = self.appInRow(current.row())
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
        self.logger.debug('activeApps: "%s"', self.activeApps)
        nextApp = None
        if len(self.activeApps) >= 1:
            rowId = self.rowIdForAppId(self.activeApps[0].appid)
            nextApp = self.nextAppWithDrops(startAt=rowId+1)
            self.logger.debug('nextApp: "%s"', nextApp)
            if nextApp:
                # Update icon of old statusCell
                self.tableWidgetGames.item(rowId, 0).setIcon(QIcon())

                # Load the next app into idle thread
                self.startIdle(nextApp)
                if rowId + 1 == self.tableWidgetGames.rowCount() - 1:
                    # This was the last app(/row), disable next button
                    self.actionNext.setEnabled(False)

        if nextApp == None:
            # No row with this id: stop
            self.logger.debug('No next app found. stop idle')
            self.stopIdle()
            self.updateSteamData() # This will update the table and enable/disable buttons as needed

    @pyqtSlot(App)
    def on_multiIdleAppDone(self, app):
        self.logger.debug('activeApps: "%s"', self.activeApps)
        self.activeApps.remove(app)
        self.logger.debug('activeApps: "%s"', self.activeApps)
        rowId = self.rowIdForAppId(app.appid)
        self.logger.debug('on_multiIdleAppDone, removing icon from row: %d', rowId)
        self.tableWidgetGames.item(rowId, 0).setIcon(QIcon()) # Remove "running" icon from app
        self.on_actionRefresh_triggered()

    @pyqtSlot(int, int)
    def on_tableWidgetGames_cellDoubleClicked(self, row, column):
        # FIXME: cellDoubleClicked left click only
        def _startClickedApp():
            try:
                self._idleThread.finished.disconnect(_startClickedApp)
                self._multiIdleThread.finished.disconnect(_startClickedApp)
            except (TypeError, AttributeError):
                pass
            app = self.appInRow(row)
            self.logger.debug('startign idle on cell click request: %s', str(app))
            self.startIdle(app)

        if len(self.activeApps) == 1:
            # Stop currently ideling game
            self.logger.debug('stop idle')
            self.stopIdle()
            self._idleThread.finished.connect(_startClickedApp)
        elif len(self.activeApps) > 1:
            # Stop MultiIdle
            self.logger.debug('stop multiidle')
            self.stopMultiIdle()
            self._multiIdleThread.finished.connect(_startClickedApp)
        else:
            # Nothing running
            _startClickedApp()

    @pyqtSlot()
    def on_actionSettings_triggered(self):
        self.showSettings()

    @pyqtSlot()
    def on_actionStartStopMultiIdle_triggered(self):
        if len(self.activeApps) == 1:
            # Something is running, stop
            self.logger.debug('stop idle')
            self.stopIdle()
        elif len(self.activeApps) > 1:
            # Stop Multi-Idleif enabled
            self.logger.debug('stop multiidle')
            self.stopMultiIdle()
        else:
            # Nothing is running
            # Start Multi-Idle if enabled
            self.logger.debug('start multiidle')
            self.startMultiIdle()

    @pyqtSlot('QPoint')
    def on_tableWidgetGames_customContextMenuRequested(self, pos):
        idx = self.tableWidgetGames.indexAt(pos)
        self.logger.debug('%s %s', idx.row(), idx.column())
        app = self.appInRow(idx.row())
        self.logger.debug(str(app))

        def _stopIdle():
            self.logger.info('stopping idle %s', app)
            if len(self.activeApps) == 1:
                # Something is running, stop
                self.logger.debug('stop idle')
                self.stopIdle()
            elif len(self.activeApps) > 1:
                # Stop Multi-Idleif enabled
                self.logger.debug('stop multiidle')
                self.stopMultiIdle()

        def _openBadgeProgress():
            self.logger.info('Opening badge progress page for %s', app)
            QDesktopServices.openUrl(QUrl('https://steamcommunity.com/my/gamecards/%d'%app.appid))

        menu = QMenu(self)
        if app in self.activeApps:
            menu.addAction(QIcon.fromTheme(_fromUtf8('media-playback-stop')),
                            'Stop idle',
                            _stopIdle)
        else:
            menu.addAction(QIcon.fromTheme(_fromUtf8('media-playback-start')),
                            'Start idle',
                            lambda: self.startIdle(app))
        menu.addSeparator()
        menu.addAction('Show badge progress', _openBadgeProgress)
        # TODO: Add to blacklist option
        p = QPoint(pos)
        p.setY(p.y() + menu.height())
        where = self.tableWidgetGames.mapToGlobal(p)
        menu.exec_(where)

    @pyqtSlot()
    def _updateLabelStatusBarTimer(self):
        self._statusBarTimerDelta -= timedelta(seconds=1)
        self.labelStatusBarTimer.setText('Next Update: %s' % self._statusBarTimerDelta)

    @pyqtSlot(int)
    def on_SteamParser_startTimer(self, interval):
        if self._statusBarTimer:
            self.on_SteamParser_stopTimer()
        self.logger.debug(interval)
        self._statusBarTimerDelta = timedelta(seconds=interval/1000)
        self.labelStatusBarTimer.setText('Next Update: %s' % self._statusBarTimerDelta)
        self._statusBarTimer = QTimer()
        self._statusBarTimer.timeout.connect(self._updateLabelStatusBarTimer)
        self._statusBarTimer.start(1*1000)

    @pyqtSlot()
    def on_SteamParser_stopTimer(self):
        self.logger.debug('Stopping timer and hiding labelStatusBarTimer')
        self._statusBarTimer.stop()
        self._statusBarTimer = None
        self.labelStatusBarTimer.clear()
