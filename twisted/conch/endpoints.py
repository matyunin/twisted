# -*- test-case-name: twisted.conch.test.test_endpoints -*-
# Copyright (c) Twisted Matrix Laboratories.
# See LICENSE for details.

"""
Endpoint implementations of various SSH interactions.
"""

__all__ = ['AuthenticationFailed', 'SSHCommandAddress', 'SSHCommandEndpoint']

from os.path import expanduser

from zope.interface import implementer

from twisted.python.filepath import FilePath
from twisted.python.failure import Failure
from twisted.internet.error import ConnectionDone
from twisted.internet.interfaces import IStreamClientEndpoint
from twisted.internet.protocol import Factory
from twisted.internet.defer import Deferred, succeed
from twisted.internet.endpoints import TCP4ClientEndpoint

from twisted.conch.ssh.keys import Key
from twisted.conch.ssh.common import NS
from twisted.conch.ssh.transport import SSHClientTransport
from twisted.conch.ssh.connection import SSHConnection
from twisted.conch.ssh.userauth import SSHUserAuthClient
from twisted.conch.ssh.channel import SSHChannel
from twisted.conch.client.knownhosts import KnownHostsFile
from twisted.conch.client.agent import SSHAgentClient

class AuthenticationFailed(Exception):
    """
    An SSH session could not be established because authentication was not
    successful.
    """



class SSHCommandAddress(object):
    def __init__(self, server, username, command):
        """
        @param server: The address of the SSH server on which the command is
            running.
        @type server: L{IAddress} provider

        @param username:
        """
        self.server = server
        self.username = username
        self.command = command



class _CommandChannel(SSHChannel):
    name = 'session'

    def __init__(self, command, protocolFactory, commandConnected):
        SSHChannel.__init__(self)
        self._command = command
        self._protocolFactory = protocolFactory
        self._commandConnected = commandConnected


    def openFailed(self, reason):
        self._commandConnected.errback(reason)


    def channelOpen(self, ignored):
        command = self.conn.sendRequest(
            self, 'exec', NS(self._command), wantReply=True)
        command.addCallbacks(self._execSuccess, self._execFailure)


    def _execFailure(self, reason):
        self._commandConnected.errback(reason)


    def _execSuccess(self, result):
        self._protocol = self._protocolFactory.buildProtocol(
            SSHCommandAddress(
                self.conn.transport.transport.getPeer(),
                self.conn.transport.factory.username,
                self.conn.transport.factory.command))
        self._protocol.makeConnection(self)
        self._commandConnected.callback(self._protocol)


    def dataReceived(self, bytes):
        self._protocol.dataReceived(bytes)


    def closed(self):
        self.conn.transport.loseConnection()
        self._protocol.connectionLost(
            Failure(ConnectionDone("ssh channel closed")))



class _ConnectionReady(SSHConnection):
    def __init__(self, factory):
        SSHConnection.__init__(self)
        self._factory = factory


    def serviceStarted(self):
        d, self._factory.connectionReady = self._factory.connectionReady, None
        d.callback(self)



class UserAuth(SSHUserAuthClient):
    password = None
    keys = None
    agent = None

    def getPublicKey(self):
        if self.agent is not None:
            return self.agent.getPublicKey()

        if self.keys:
            self.key = self.keys.pop(0)
        else:
            self.key = None
        return self.key.public()


    def signData(self, publicKey, signData):
        """
        Extend the base signing behavior by using an SSH agent to sign the
        data, if one is available.

        @type publicKey: L{Key}
        @type signData: C{str}
        """
        if self.agent is not None:
            return self.agent.signData(publicKey.blob(), signData)
        else:
            return SSHUserAuthClient.signData(self, publicKey, signData)


    def getPrivateKey(self):
        return succeed(self.key)


    def getPassword(self):
        return succeed(self.password)


    def ssh_USERAUTH_SUCCESS(self, packet):
        self.transport._state = b'CHANNELLING'
        return SSHUserAuthClient.ssh_USERAUTH_SUCCESS(self, packet)



class _CommandTransport(SSHClientTransport):
    _state = b'STARTING' # -> b'SECURING' -> b'AUTHENTICATING' -> b'CHANNELLING' -> b'RUNNING'

    _hostKeyFailure = None

    def verifyHostKey(self, hostKey, fingerprint):
        hostname = self.factory.hostname
        ip = self.transport.getPeer().host

        self._state = b'SECURING'
        d = self.factory.knownHosts.verifyHostKey(
            self.factory.ui, hostname, ip, Key.fromString(hostKey))
        d.addErrback(self._saveHostKeyFailure)
        return d


    def _saveHostKeyFailure(self, reason):
        self._hostKeyFailure = reason
        return reason


    def connectionSecure(self):
        self._state = b'AUTHENTICATING'

        command = _ConnectionReady(self.factory)

        userauth = UserAuth(self.factory.username, command)
        userauth.password = self.factory.password
        if self.factory.keys:
            userauth.keys = list(self.factory.keys)

        if self.factory.agentEndpoint is not None:
            d = self._connectToAgent(userauth, self.factory.agentEndpoint)
        else:
            d = succeed(None)

        def maybeGotAgent(ignored):
            self.requestService(userauth)
        d.addBoth(maybeGotAgent)


    def _connectToAgent(self, userauth, endpoint):
        factory = Factory()
        factory.protocol = SSHAgentClient
        d = endpoint.connect(factory)
        def connected(agent):
            userauth.agent = agent
            return agent.getPublicKeys()
        d.addCallback(connected)
        return d


    def connectionLost(self, reason):
        if self._state == b'RUNNING' or self.factory.connectionReady is None:
            return
        if self._state == b'SECURING' and self._hostKeyFailure is not None:
            reason = self._hostKeyFailure
        elif self._state == b'AUTHENTICATING':
            reason = Failure(AuthenticationFailed("Doh"))
        self.factory.connectionReady.errback(reason)



@implementer(IStreamClientEndpoint)
class SSHCommandEndpoint(object):
    """
    """

    @classmethod
    def newConnection(cls, reactor, command, username, hostname, port=None, keys=None, password=None, agentEndpoint=None, knownHosts=None, ui=None):
        """
        @param reactor: The reactor to use to establish the connection.
        @type reactor: L{IReactorTCP} provider

        @param command: The command line to execute on the SSH server.
        @type command: L{bytes}

        @param username: The username with which to authenticate to the SSH
            server.
        @type username: L{bytes}

        @param hostname: The hostname of the SSH server.
        @type hostname: L{bytes}

        @param port: The port number of the SSH server.  By default, the
            standard SSH port number is used.
        @type port: L{int}

        @param keys: Private keys with which to authenticate to the SSH server,
            if key authentication is to be attempted (otherwise C{None}).
        @type keys: L{list} of L{Key}

        @param password: The password with which to authenticate to the SSH
            server, if password authentication is to be attempted (otherwise
            C{None}).
        @type password: L{bytes} or L{NoneType}

        @param agentEndpoint: An L{IStreamClientEndpoint} provider which may be
            used to connect to an SSH agent, if one is to be used to help with
            authentication.
        @type agentEndpoint: L{IStreamClientEndpoint} provider

        @param knownHosts: The currently known host keys, used to check the
            host key presented by the server we actually connect to.
        @type knownHosts: L{KnownHostsKey}

        @param ui: An object for interacting with users to make decisions about
            whether to accept the server host keys.
        @type ui: L{ConsoleUI}
        """
        self = _NewConnectionHelper()
        self.reactor = reactor
        self.hostname = hostname
        self.port = port
        self.command = command
        self.username = username
        self.keys = keys
        self.password = password
        self.agentEndpoint = agentEndpoint
        if knownHosts is None:
            knownHosts = self._knownHosts()
        self.knownHosts = knownHosts
        self.ui = ui

        endpoint = cls.__new__(cls)
        endpoint.params = self
        return endpoint


    @classmethod
    def existingConnection(cls, connection, command):
        endpoint = cls.__new__(cls)
        endpoint.params = _ExistingConnectionHelper(connection)
        endpoint.params.command = command
        return endpoint


    def connect(self, protocolFactory):
        """
        Set up an SSH connection, use a channel from that connection to launch
        a command, and hook the stdin and stdout of that command up as a
        transport for a protocol created by the given factory.

        @param protocolFactory: A L{Factory} to use to create the protocol
            which will be connected to the stdin and stdout of the command on
            the SSH server.

        @return: A L{Deferred} which will fire with an error if the connection
            cannot be set up for any reason or with the protocol instance
            created by C{protocolFactory} once it has been connected to the
            command.
        """
        d = self.params.secureConnection()
        d.addCallback(self._executeCommand, protocolFactory)
        return d


    def _executeCommand(self, connection, protocolFactory):
        commandConnected = Deferred()
        def disconnectOnFailure(passthrough):
            connection.transport.loseConnection()
            return passthrough
        commandConnected.addErrback(disconnectOnFailure)

        channel = _CommandChannel(
            self.params.command, protocolFactory, commandConnected)
        connection.openChannel(channel)
        return commandConnected



class _NewConnectionHelper(object):
    _KNOWN_HOSTS = "~/.ssh/known_hosts"

    @classmethod
    def _knownHosts(cls):
        """
        Create and return a L{KnownHostsFile} instance pointed at the user's
        personal I{known hosts} file.
        """
        return KnownHostsFile.fromPath(FilePath(expanduser(cls._KNOWN_HOSTS)))


    def secureConnection(self):
        factory = Factory()
        factory.protocol = _CommandTransport
        factory.hostname = self.hostname
        factory.username = self.username
        factory.keys = self.keys
        factory.password = self.password
        factory.agentEndpoint = self.agentEndpoint
        factory.knownHosts = self.knownHosts
        factory.command = self.command
        factory.ui = self.ui

        factory.connectionReady = Deferred()

        sshClient = TCP4ClientEndpoint(self.reactor, self.hostname, self.port)

        d = sshClient.connect(factory)
        d.addCallback(lambda ignored: factory.connectionReady)
        return d



class _ExistingConnectionHelper(object):
    def __init__(self, connection):
        self.connection = connection


    def secureConnection(self):
        return succeed(self.connection)