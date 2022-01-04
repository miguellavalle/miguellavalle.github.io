.. title: Implementing multiple bindings for OpenStack Networking ports
.. slug: neutron-multiple-port-bindings
.. date: 2017-12-08 15:53:16 UTC-06:00
.. tags: OpenStack, Neutron
.. category: 
.. link: 
.. description: 
.. type: text

One would be hard pressed to point out a more fundamental function of OpenStack
Networking (a.k.a. Neutron) than that of providing virtual ports and the
process of binding them. It is through bound ports that virtual machines
(instances) and higher level services like load balancers can access the
virtual networking provided by Neutron. In this blog post I want to provide an
overview of that process we call port binding and then explain how and why we
are implementing multiple port bindings to better support the migration of Nova
instances. This work is being done for the ML2 plug-in, which is the reference
Neutron core plug-in. The upshot is that all the ML2 based mechanism drivers
that are not part of the Neutron code repository will also be enabled with
multiple port bindings.

ML2 port binding
================

A port is an access point to a Neutron virtual network. Virtual machines, bare
metal servers and higher level services use ports to send and receive data over
a virtual network. Binding is the process whereby the ML2 core plug-in decides
how a port is going to be connected physically to the network to which it
belongs.

One of the key design goals for ML2 was to support heterogeneous networking
technology across compute nodes. A simple example can be a deployment where
some of the compute nodes use OVS while others use Linux Bridge. To support
such heterogeneity, ML2 offers the concept of mechanism drivers. In our example
deployment, we would configure two mechanism drivers: the OVS driver and the
Linux Bridge driver. A mechanism driver is responsible of configuring the
physical infrastructure (OVS or Linux Bridge in our example) to bind ports to
that infrastructure so they can access their virtual network. The binding
process has a set of well defined inputs:

* Port attributes.

  - ``binding:host_id``. This is a string specifying the name of the host where
    the port should be bound.
  - ``binding:vnic_type``.  A Neutron port can be requested to be bound as a
    virtual NIC (OVS, Linux Bridge) , direct pci-passthrough, direct macvtap
    or other types. A mechanism driver only binds a port if it supports its
    ``vnic_type``.
  - ``binding:profile``. This is a dictionary of key / value pairs that
    provides information used to influence the binding process. ML2 will
    accept, store, and return arbitrary key / value pairs within the
    dictionary and their semantics are mechanism driver dependent.

* Network infrastructure configuration. ML2 carries out the process of binding
  a port outside any DB transaction. This is to allow the mechanism drivers to
  configure the infrastructure by performing blocking calls.

ML2 will attempt a binding when, during the processing of a ReST API port
create or update call, it finds that ``binding:host_id`` is defined (not
``None``) and ``binding:vif_type`` has a value of ``VIF_TYPE_UNBOUND`` or
``VIF_TYPE_BINDING_FAILED``. Under these conditions, ML2 calls the
``bind_port`` method of each mechanism driver in the order in which they are
configured in the mechanism_drivers option in the ``ml2`` section of
``/etc/neutron/plugins/ml2/ml2_conf.ini``. This process continues until one of
the drivers binds the port successfully to one of the network segments or all
the drivers have been called and have failed, in which case
``binding:vif_type`` is set to ``VIF_TYPE_BINDING_FAILED``. Failure to bind
doesn't mean the ReST API call fails. A port create or update can still succeed
even though it was not possible to bind it.

There is much more to be said about port binding that goes beyond the scope of
this post. To learn more about the subject, you can watch the master on the
subject, Robert Kukura, giving a presentation during the 2016 OpenStack Summit
in Austin [1]_.

Better support for instance live migration with multiple port bindings
======================================================================

Instance live migration consists of three stages:

#. ``pre_live_migration``. Executed before migration starts. The target host
   is determined in this stage, but the instance still resides on the source.
#. ``live_migration_operation``. This is the stage where the instance is moved
   to the target host.
#. ``post_live_migration``. The migration has concluded and the source instance
   doesn't exist anymore. Up until now, this is the stage where the instance
   ports were bound.

The problem with this flow is that the port binding happens too late:

* If the binding fails, all the migration steps taken up to this point are
  wasted and the instance gets stuck in an error state. If the migration is
  going to fail due to port binding, we want it to happen as early as possible
  in the process, preferably before the instance is migrated.
* Network downtime is lengthened by binding the ports in the
  ``post_live_migration`` stage, since the source instance is removed before
  the binding starts.
* The destination instance definition is built using the results of the source
  instance bindings (``vif_type`` and ``vif_details``). This prevents the
  possibility of migrating the instance between hosts with different networking 
  technology, from OVS to Linux bridge for example (or to a new and promising
  technology).

To address these issues, the destination instance port binding creation needs
to be moved to the ``pre_live_migration`` stage. But since the source instance
is still alive at this stage and using its ports bindings, we need to be able
to associate more than one binding with each port. The outline of the solution
is the following:

* A port can have more than one binding. Each binding corresponds to a specific
  host.
* Only one binding will be in ``PORT_BINDING_STATUS_ACTIVE``. The others will
  be in ``PORT_BINDING_STATUS_INACTIVE``.
* The Neutron ReST API is extended to support CRUD operations for multiple port
  bindings. Also an ``activate binding`` operation is added.
* When live migrating an instance, Nova will use the new ReST API extension to:

  - Create during pre_live_migration new bindings in
    ``PORT_BINDING_STATUS_INACTIVE``.
  - Use information gathered from the inactive binding to modify the instance
    definition during the ``live_migration_operation``.
  - Use the ``activate`` operation to set the source instance bindings to
    ``PORT_BINDING_STATUS_INACTIVE`` and the destination instance bindings to
    ``PORT_BINDING_STATUS_ACTIVE`` once the latter instance becomes active
    during the ``live_migration_operation``.
  - Remove the inactive bindings on the source compute host during the
    ``post_live_migration``.

The specific calls in the new ReST API extension are the following (for details
on the calls requests and responses please see [2]_):

* ``POST /v2.0/ports/{port_id}/bindings``. The request body has to specify the
  ``host`` to which the binding will be associated and can specify optionally a
  ``vnic_type`` and a ``profile``. The call returns ``vif_type`` and
  ``vif_details`` and results in a 500 code if the binding fails. In the
  current implementation, only instance ports (``device_owner`` ==
  ``const.DEVICE_OWNER_COMPUTE_PREFIX``) can have multiple port bindings and a
  maximum of 2 bindings are allowed per port.
* ``PUT /v2.0/ports/{port_id}/bindings/{host_id}``. Allows the caller to update
  the ``vnic_type`` and ``profile``. It returns a new ``vif_type`` and
  ``vif_details`` and results in 500 if the binding fails.
* ``PUT /v2.0/ports/{port_id}/bindings/{host_id}/activate``. When applied to an
  inactivate binding, it will activate it and inactivate the previously active
  one. Attempting to activate an existing active binding will return a 400. It
  will return a 500 if the binding fails.
* ``GET /v2.0/ports/{port_id}/bindings``. Returns the bindings associated to a
  port.
* ``GET /v2.0/ports/{port_id}/bindings/{host_id}``. Returns the details of a
  specific binding.

Conclusion
==========

I have given an overview of the port binding process and how it is being
improved to better support instance live migration. This is the latest chapter
in a long history of cooperation between the Neutron and Nova teams that will
continue in the future in our joint quest to better support OpenStack users.
Please see the specification in [3]_ to learn how the Nova team is leveraging
multiple port bindings.

References
----------

.. [1] Understanding ML2 Port Binding:
       https://www.youtube.com/watch?v=e38XM-QaA5Q&t=1801s
.. [2] Provide Port Binding Information for Nova Live Migration specification:
       https://specs.openstack.org/openstack/neutron-specs/specs/backlog/pike/portbinding_information_for_nova.html
.. [3] Use Neutron’s new port binding API specification:
       https://specs.openstack.org/openstack/nova-specs/specs/queens/approved/neutron-new-port-binding-api.html
